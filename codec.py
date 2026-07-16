"""
Codec detection and conversion for SporkMP3.
Handles xHE-AAC (USAC) and other incompatible codecs by transcoding
to a format that discord.py's FFmpegPCMAudio can handle.

Decoder strategy for xHE-AAC:
  1. Try libfdk_aac decoder if available (best quality, license-restricted)
  2. Fall back to native FFmpeg decoder with -strict experimental
  3. Validate output: duration sanity check + silence detection
"""
import asyncio
import json
import logging
import os
import struct
from typing import Optional, Tuple


# Codecs that FFmpegPCMAudio can decode natively (no conversion needed)
NATIVE_CODECS = {
    # PCM / lossless
    'pcm_s16le', 'pcm_s16be', 'pcm_s24le', 'pcm_s24be',
    'pcm_s32le', 'pcm_s32be', 'pcm_f32le', 'pcm_f64le',
    'flac', 'alac', 'wavpack',
    # Lossy – widely supported
    'mp3', 'mp2', 'aac', 'vorbis', 'opus',
    'wmav1', 'wmav2', 'adpcm_ms', 'adpcm_ima_wav',
}

# Codecs that REQUIRE conversion (known problematic)
CONVERT_CODECS = {
    'usac',          # xHE-AAC / USAC — the main target
    'als',           # ALS lossless (rare, MPEG-4)
    'sls',           # SLS scalable lossless
}

# Supported input container extensions (the formats SporkMP3 accepts)
SUPPORTED_EXTENSIONS = {'.mp3', '.wav', '.ogg', '.flac', '.m4a', '.aac', '.mp4', '.webm'}

# Cache for decoder availability (checked once per process)
_decoder_cache: dict = {}


# ============================================================================
# DECODER AVAILABILITY
# ============================================================================

async def _check_decoder(decoder_name: str) -> bool:
    """Check whether a specific FFmpeg decoder is available on this system."""
    if decoder_name in _decoder_cache:
        return _decoder_cache[decoder_name]

    try:
        proc = await asyncio.create_subprocess_exec(
            'ffmpeg', '-hide_banner', '-decoders',
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
        available = decoder_name in stdout.decode(errors='replace')
        _decoder_cache[decoder_name] = available
        logging.info(f"Decoder '{decoder_name}' available: {available}")
        return available
    except Exception as e:
        logging.warning(f"Could not check decoder availability: {e}")
        _decoder_cache[decoder_name] = False
        return False


async def has_fdk_aac() -> bool:
    """Check if libfdk_aac decoder is available (best xHE-AAC quality)."""
    return await _check_decoder('libfdk_aac')


# ============================================================================
# PROBING
# ============================================================================

async def probe_codec(file_path: str) -> Optional[str]:
    """
    Use ffprobe to detect the audio codec of a file.
    Returns the codec name (e.g. 'aac', 'mp3', 'usac') or None on failure.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            'ffprobe',
            '-v', 'quiet',
            '-select_streams', 'a:0',
            '-show_entries', 'stream=codec_name',
            '-of', 'json',
            file_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=15)

        if proc.returncode != 0:
            logging.warning(f"ffprobe failed for {file_path}: {stderr.decode(errors='replace')}")
            return None

        data = json.loads(stdout.decode())
        streams = data.get('streams', [])
        if streams:
            codec = streams[0].get('codec_name', '').lower()
            logging.debug(f"Probed codec for {file_path}: {codec}")
            return codec
        return None

    except asyncio.TimeoutError:
        logging.error(f"ffprobe timed out for {file_path}")
        return None
    except Exception as e:
        logging.error(f"ffprobe error for {file_path}: {e}")
        return None


async def probe_format_details(file_path: str) -> dict:
    """
    Get extended probe info: codec, sample_rate, channels, bit_rate, duration.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            'ffprobe',
            '-v', 'quiet',
            '-select_streams', 'a:0',
            '-show_entries', 'stream=codec_name,sample_rate,channels,bit_rate,codec_long_name,profile',
            '-show_entries', 'format=duration,format_name',
            '-of', 'json',
            file_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=15)

        if proc.returncode != 0:
            return {}

        data = json.loads(stdout.decode())
        result = {}

        streams = data.get('streams', [])
        if streams:
            s = streams[0]
            result['codec'] = s.get('codec_name', '').lower()
            result['codec_long'] = s.get('codec_long_name', '')
            result['sample_rate'] = int(s.get('sample_rate', 0))
            result['channels'] = int(s.get('channels', 0))
            result['bit_rate'] = int(s.get('bit_rate', 0)) if s.get('bit_rate') else None
            result['profile'] = s.get('profile', '').strip()

        fmt = data.get('format', {})
        result['duration'] = float(fmt.get('duration', 0))
        result['format_name'] = fmt.get('format_name', '')

        return result

    except Exception as e:
        logging.error(f"Detailed probe failed for {file_path}: {e}")
        return {}


# ============================================================================
# VALIDATION
# ============================================================================

async def _detect_mean_volume(file_path: str) -> Optional[float]:
    """
    Use ffmpeg volumedetect to get mean volume of the output file.
    Returns mean_volume in dB, or None on failure.
    A fully silent file reads around -91 dB or -inf.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            'ffmpeg',
            '-i', file_path,
            '-af', 'volumedetect',
            '-f', 'null', '-',
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
        output = stderr.decode(errors='replace')

        for line in output.splitlines():
            if 'mean_volume' in line:
                parts = line.split('mean_volume:')
                if len(parts) == 2:
                    vol_str = parts[1].strip().replace('dB', '').strip()
                    if vol_str == '-inf':
                        return -999.0
                    return float(vol_str)
        return None
    except Exception as e:
        logging.warning(f"Volume detection failed for {file_path}: {e}")
        return None


async def _validate_conversion(
    input_path: str,
    output_path: str,
    source_duration: float,
) -> Tuple[bool, str]:
    """
    Validate converted output isn't corrupt, silent, or truncated.
    Returns (is_valid, reason).
    """
    # 1. File exists and isn't empty
    if not os.path.exists(output_path):
        return False, "Output file does not exist"
    if os.path.getsize(output_path) == 0:
        return False, "Output file is empty (0 bytes)"

    # 2. Duration sanity check (allow 2s or 10% drift)
    out_details = await probe_format_details(output_path)
    out_duration = out_details.get('duration', 0)

    if source_duration > 0 and out_duration > 0:
        drift = abs(out_duration - source_duration)
        pct = drift / source_duration
        if drift > 2.0 and pct > 0.10:
            return False, (
                f"Duration mismatch: source={source_duration:.1f}s, "
                f"output={out_duration:.1f}s (drift={pct:.0%})"
            )

    # 3. Silence detection — catch garbled/zeroed output
    mean_vol = await _detect_mean_volume(output_path)
    if mean_vol is not None and mean_vol < -80.0:
        return False, f"Output appears silent (mean_volume={mean_vol:.1f} dB)"

    return True, "OK"


def _read_aac_object_type(file_path: str) -> Optional[int]:
    """
    Read the Audio Object Type directly from an MP4/M4A file's esds atom.

    This is ffprobe-version-independent: AOT 42 = USAC/xHE-AAC regardless
    of how ffprobe labels the codec_name or profile fields.

    Returns the AOT number (e.g. 2=AAC-LC, 42=USAC) or None if unreadable.
    """
    try:
        # Find the 'moov' box by parsing top-level MP4 boxes
        moov_offset = None
        moov_size = None

        with open(file_path, 'rb') as f:
            file_size = f.seek(0, 2)
            f.seek(0)

            pos = 0
            while pos < file_size:
                f.seek(pos)
                header = f.read(8)
                if len(header) < 8:
                    break

                box_size, box_type = struct.unpack('>I4s', header)

                if box_size == 0:           # box extends to EOF
                    box_size = file_size - pos
                elif box_size == 1:          # 64-bit extended size
                    ext = f.read(8)
                    if len(ext) < 8:
                        break
                    box_size = struct.unpack('>Q', ext)[0]

                if box_type == b'moov':
                    moov_offset = pos
                    moov_size = box_size
                    break

                if box_size < 8:             # prevent infinite loop
                    break
                pos += box_size

        if moov_offset is None:
            return None

        # Read the moov box and search for esds within it
        with open(file_path, 'rb') as f:
            f.seek(moov_offset)
            moov_data = f.read(min(moov_size, 2 * 1024 * 1024))  # cap at 2 MB

        idx = moov_data.find(b'esds')
        if idx < 0:
            return None

        # esds box: 4-byte type already consumed by find; skip version+flags
        pos = idx + 4 + 4

        def _read_desc_header(data, p):
            """Read MPEG-4 descriptor tag + variable-length size."""
            if p >= len(data):
                return None, 0, p
            tag = data[p]; p += 1
            length = 0
            for _ in range(4):
                if p >= len(data):
                    return tag, length, p
                b = data[p]; p += 1
                length = (length << 7) | (b & 0x7F)
                if not (b & 0x80):
                    break
            return tag, length, p

        # ES_Descriptor (tag 0x03) → skip ES_ID(2) + flags(1)
        tag, _, pos = _read_desc_header(moov_data, pos)
        if tag != 0x03:
            return None
        pos += 3

        # DecoderConfigDescriptor (tag 0x04) → skip 13 bytes
        tag, _, pos = _read_desc_header(moov_data, pos)
        if tag != 0x04:
            return None
        pos += 13

        # DecoderSpecificInfo (tag 0x05) → AudioSpecificConfig
        tag, _, pos = _read_desc_header(moov_data, pos)
        if tag != 0x05:
            return None

        if pos + 1 >= len(moov_data):
            return None

        # AudioObjectType: first 5 bits (extended form if == 31)
        aot = (moov_data[pos] >> 3) & 0x1F
        if aot == 31:
            aot = 32 + (((moov_data[pos] & 0x07) << 3) | ((moov_data[pos + 1] >> 5) & 0x07))

        return aot

    except Exception as e:
        logging.debug(f"Failed to read AAC object type from {file_path}: {e}")
        return None


# USAC Audio Object Type (MPEG-D Unified Speech and Audio Coding)
_AOT_USAC = 42


def _is_xhe_aac_file(file_path: str) -> bool:
    """
    Detect xHE-AAC / USAC by reading the Audio Object Type from the MP4 container.
    Returns True if AOT == 42 (USAC).
    """
    aot = _read_aac_object_type(file_path)
    if aot == _AOT_USAC:
        logging.info(f"xHE-AAC detected (AOT {aot}) in {file_path}")
        return True
    if aot is not None:
        logging.debug(f"AAC AOT {aot} (not USAC) in {file_path}")
    return False


def needs_conversion(codec_name: Optional[str]) -> bool:
    """
    Determine whether a codec needs transcoding.
    - Known native codecs → False
    - Known problematic codecs → True
    - Unknown codecs → True (safe default: convert rather than crash)
    """
    if codec_name is None:
        return True

    codec = codec_name.lower().strip()

    if codec in NATIVE_CODECS:
        return False
    if codec in CONVERT_CODECS:
        return True

    logging.info(f"Unknown codec '{codec}' — will convert to ensure compatibility")
    return True


# ============================================================================
# CONVERSION
# ============================================================================

async def _run_ffmpeg_convert(
    input_path: str,
    output_path: str,
    decoder: Optional[str],
    target_codec: str,
    bitrate: str,
    target_format: str,
) -> Tuple[bool, str]:
    """
    Run a single FFmpeg conversion attempt with the specified decoder.
    Returns (success, stderr_tail).
    """
    cmd = ['ffmpeg', '-y', '-hide_banner']

    # Force specific decoder for the input stream
    if decoder:
        cmd += ['-c:a', decoder]

    # Allow experimental decoders (native USAC needs this)
    cmd += ['-strict', 'experimental']

    cmd += [
        '-i', input_path,
        '-vn',                   # No video
        '-c:a', target_codec,    # Output codec
        '-b:a', bitrate,         # Output bitrate
        '-ar', '48000',          # Discord standard sample rate
        '-ac', '2',              # Stereo
        '-f', target_format,     # Output container
        output_path,
    ]

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)
        stderr_text = stderr.decode(errors='replace')

        if proc.returncode != 0:
            return False, stderr_text[-500:]
        return True, stderr_text[-500:]

    except asyncio.TimeoutError:
        return False, "FFmpeg timed out"
    except Exception as e:
        return False, str(e)


async def convert_to_compatible(
    input_path: str,
    output_dir: str,
    source_codec: Optional[str] = None,
    source_duration: float = 0,
    target_format: str = 'ogg',
    target_codec: str = 'libopus',
    bitrate: str = '192k',
) -> Tuple[Optional[str], Optional[float]]:
    """
    Transcode an audio file to a format compatible with discord.py.

    Uses a multi-strategy approach for xHE-AAC and other problem codecs:
      1. Try libfdk_aac decoder if available (best quality)
      2. Fall back to native decoder with -strict experimental
      3. Validate output to catch silence, truncation, or artifacts

    Returns (output_path, duration) on success, (None, None) on failure.
    """
    base = os.path.splitext(os.path.basename(input_path))[0]
    output_path = os.path.join(output_dir, f"{base}_converted.{target_format}")

    # Avoid re-converting if output already exists and passes validation
    if os.path.exists(output_path) and os.path.getsize(output_path) > 0:
        valid, reason = await _validate_conversion(input_path, output_path, source_duration)
        if valid:
            logging.info(f"Conversion cache hit (valid): {output_path}")
            details = await probe_format_details(output_path)
            return output_path, details.get('duration')
        else:
            logging.warning(f"Cached conversion invalid ({reason}), re-converting")
            os.remove(output_path)

    # Determine if source is xHE-AAC / USAC — worth trying fdk first
    is_usac = source_codec and source_codec.lower() in ('usac', 'aac')

    # Build ordered list of decoder strategies to try
    strategies = []
    if is_usac and await has_fdk_aac():
        strategies.append(('libfdk_aac', 'libfdk_aac decoder (best quality)'))
    strategies.append((None, 'native decoder (-strict experimental)'))

    last_error = ""
    for decoder, label in strategies:
        logging.info(f"Conversion attempt: {label} for {os.path.basename(input_path)}")

        # Clean up any partial output from previous attempt
        if os.path.exists(output_path):
            os.remove(output_path)

        success, stderr_tail = await _run_ffmpeg_convert(
            input_path, output_path,
            decoder=decoder,
            target_codec=target_codec,
            bitrate=bitrate,
            target_format=target_format,
        )

        if not success:
            logging.warning(f"Strategy '{label}' failed: {stderr_tail[:200]}")
            last_error = stderr_tail
            continue

        # Validate the output
        valid, reason = await _validate_conversion(input_path, output_path, source_duration)
        if not valid:
            logging.warning(f"Strategy '{label}' produced invalid output: {reason}")
            last_error = reason
            if os.path.exists(output_path):
                os.remove(output_path)
            continue

        # Success — log and return
        details = await probe_format_details(output_path)
        duration = details.get('duration')
        file_size = os.path.getsize(output_path)
        logging.info(
            f"Conversion successful ({label}): "
            f"{output_path} ({file_size} bytes, {duration:.1f}s)"
        )
        return output_path, duration

    # All strategies failed
    logging.error(f"All conversion strategies failed for {input_path}: {last_error}")
    if os.path.exists(output_path):
        os.remove(output_path)
    return None, None


# ============================================================================
# HIGH-LEVEL API
# ============================================================================

async def ensure_playable(file_path: str, temp_folder: str) -> Tuple[str, bool]:
    """
    High-level helper: probe a file and convert if necessary.

    Returns:
        (playable_path, was_converted)

    - If the codec is natively supported, returns the original path unchanged.
    - If conversion is needed and succeeds, returns the converted path.
    - If conversion fails, raises RuntimeError with diagnostic info.
    """
    codec = await probe_codec(file_path)
    logging.info(f"Codec probe: {file_path} → {codec or 'unknown'}")

    # xHE-AAC detection: ffprobe reports these as plain 'aac' codec regardless
    # of FFmpeg version.  Read the Audio Object Type from the MP4 container to
    # catch USAC (AOT 42) that would otherwise slip through as native AAC.
    force_convert = False
    if codec == 'aac' and _is_xhe_aac_file(file_path):
        logging.warning(
            f"xHE-AAC (USAC) detected in {file_path} — forcing conversion"
        )
        force_convert = True
        codec = 'usac'  # override so convert_to_compatible picks the right decoder

    if not force_convert and not needs_conversion(codec):
        return file_path, False

    # Get source details for validation and decoder selection
    details = await probe_format_details(file_path)
    codec_long = details.get('codec_long', codec or 'unknown')
    source_duration = details.get('duration', 0)

    logging.warning(
        f"Incompatible codec detected: {codec_long} in {file_path} — converting"
    )

    converted_path, duration = await convert_to_compatible(
        file_path,
        temp_folder,
        source_codec=codec,
        source_duration=source_duration,
    )
    if converted_path is None:
        raise RuntimeError(
            f"Failed to convert {os.path.basename(file_path)} "
            f"(codec: {codec_long}). The file may be corrupted or "
            f"your FFmpeg build may not support this codec. "
            f"Consider installing FFmpeg with libfdk_aac support."
        )

    return converted_path, True
