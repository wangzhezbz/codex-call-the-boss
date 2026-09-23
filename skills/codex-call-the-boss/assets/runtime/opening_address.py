"""Private, listener-selected address assets; never a new voice or a report.

The exact short recording may be reused, while every report body remains
dynamic. Preparing a candidate does not select it or claim listening approval.
"""
from array import array
import hashlib
import io
import json
import re
import wave
from pathlib import Path

FORMAT = 'intact-opening-address-v1'
TEXT = '老板，'


class OpeningBoundaryUnavailable(ValueError):
    """Valid full speech, but no safe place to replace its address."""


def digest(payload):
    return hashlib.sha256(payload).hexdigest()


def wav_bytes(pcm):
    stream = io.BytesIO()
    with wave.open(stream, 'wb') as output:
        output.setparams((1, 2, 48000, 0, 'NONE', 'not compressed'))
        output.writeframes(pcm)
    return stream.getvalue()


def read_source(cache_dir, voice, source_key, source_sha256, end_frame):
    cache_dir = Path(cache_dir)
    if any(not isinstance(v, str) or not re.fullmatch(r'[0-9a-f]{64}', v)
           for v in (source_key, source_sha256)):
        raise ValueError('Exact source identities are required')
    path = cache_dir/(source_key+'.json')
    raw = path.read_bytes()
    meta = json.loads(raw)
    source = path.with_suffix('.pcm').read_bytes()
    if (meta.get('passed') is not True or meta.get('diagnostic_only')
            or meta.get('voice') != voice or meta.get('script_similarity') != 1.0
            or not meta.get('text', '').startswith(TEXT)
            or not meta.get('model_transcript', '').startswith('老板')
            or meta.get('sha256') != source_sha256 or digest(source) != source_sha256
            or (cache_dir/'listener-rejections'/source_sha256/'rejection.json').exists()):
        raise ValueError('Address requires an intact, non-vetoed same-voice opening')
    with wave.open(str(path.with_suffix('.wav')), 'rb') as stream:
        if stream.getparams()[:3] != (1, 2, 48000) or stream.readframes(stream.getnframes()) != source:
            raise ValueError('Source WAV differs from PCM')
    if type(end_frame) is not int or not 19200 <= end_frame <= 48000 or end_frame + 960 >= len(source)//2:
        raise ValueError('Address boundary must be explicitly reviewed between 400 and 1000 ms')
    around = array('h', source[(end_frame-960)*2:(end_frame+960)*2])
    if max(map(abs, around), default=32768) > 256:
        raise ValueError('Address boundary cuts nonquiet audio')
    pcm = source[:end_frame*2]
    return pcm, {'kind': FORMAT, 'voice': voice, 'text': TEXT,
                 'source_key': source_key, 'source_pcm_sha256': source_sha256,
                 'source_metadata_sha256': digest(raw), 'start_frame': 0,
                 'end_frame': end_frame, 'pcm_sha256': digest(pcm)}


def asset_dir(cache_dir, pcm_sha256):
    if not isinstance(pcm_sha256, str) or not re.fullmatch(r'[0-9a-f]{64}', pcm_sha256):
        raise ValueError('Invalid fixed-address identity')
    return Path(cache_dir)/'opening-addresses'/pcm_sha256


def load(cache_dir, voice, pcm_sha256, *, require_acceptance=True):
    folder = asset_dir(cache_dir, pcm_sha256)
    raw = (folder/'asset.json').read_bytes()
    asset = json.loads(raw)
    if (asset.get('kind') != FORMAT or asset.get('voice') != voice or asset.get('text') != TEXT
            or asset.get('pcm_sha256') != pcm_sha256 or asset.get('quality', {}).get('passed') is not True
            or asset.get('quality', {}).get('script_similarity') != 1.0):
        raise ValueError('Invalid or unvalidated fixed-address asset')
    pcm, proof = read_source(cache_dir, voice, asset['source_key'], asset['source_pcm_sha256'], asset['end_frame'])
    if any(asset.get(k) != v for k, v in proof.items()) or (folder/'address.pcm').read_bytes() != pcm:
        raise ValueError('Fixed-address source or PCM changed')
    if (Path(cache_dir)/'listener-rejections'/pcm_sha256/'rejection.json').exists():
        raise ValueError('The fixed address has a listener veto')
    if require_acceptance:
        accepted = json.loads((folder/'acceptance.json').read_bytes())
        if (accepted.get('kind') != 'explicit_listener_acceptance'
                or accepted.get('scope') != 'opening_address_only'
                or accepted.get('pcm_sha256') != pcm_sha256
                or accepted.get('asset_sha256') != digest(raw)):
            raise ValueError('Exact address has not been accepted by the listener')
    return pcm, {'revision': FORMAT, 'pcm_sha256': pcm_sha256, 'asset_sha256': digest(raw)}


def report_body_start(pcm):
    """Choose one conservative quiet boundary, then require suffix/full QA.

    Do not trust coarse transcript milliseconds as sample offsets or try
    successive cuts until ASR happens to pass. No valid pause means reject.
    """
    samples = array('h', pcm)
    run = None
    for start in range(19200, min(62400, len(samples)-1920), 960):
        quiet = max(map(abs, samples[start:start+960]), default=32768) <= 256
        if quiet and run is None:
            run = start
        if not quiet and run is not None:
            if start-run >= 3840:
                return ((run+start)//2//960)*960
            run = None
    raise OpeningBoundaryUnavailable('No complete quiet address/body boundary; preserve the original clip')
