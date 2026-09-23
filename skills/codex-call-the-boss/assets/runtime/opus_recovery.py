"""Per-receiver Opus FEC/PLC for the pinned aiortc 1.15 adapter.

Conceals bounded sequence losses, never arbitrary RTP timestamp jumps. FEC
uses the following packet's redundancy when present; otherwise libopus uses
PLC. Concealment is not recovery of the original missing signal.
"""
from __future__ import annotations
import asyncio
import ctypes as C
import ctypes.util
import threading
from fractions import Fraction
from types import MethodType

import aiortc
from aiortc.codecs import get_decoder
from av import AudioFrame


def opus_library():
    name = ctypes.util.find_library('opus')
    if not name:
        raise RuntimeError('Native speech loss concealment needs the installed libopus library')
    lib = C.CDLL(name)
    lib.opus_decoder_create.argtypes = [C.c_int,C.c_int,C.POINTER(C.c_int)]
    lib.opus_decoder_create.restype = C.c_void_p
    lib.opus_decoder_destroy.argtypes = [C.c_void_p]
    lib.opus_decoder_init.argtypes = [C.c_void_p,C.c_int,C.c_int]
    lib.opus_decode.argtypes = [C.c_void_p,C.c_char_p,C.c_int,C.POINTER(C.c_int16),C.c_int,C.c_int]
    lib.opus_decode.restype = C.c_int
    lib.opus_packet_get_nb_samples.argtypes = [C.c_char_p,C.c_int,C.c_int]
    return lib


class OpusRecoveryDecoder:
    def __init__(self, metrics=None):
        self.lib = opus_library()
        error = C.c_int()
        self.pointer = self.lib.opus_decoder_create(48000,1,C.byref(error))
        if error.value or not self.pointer:
            raise RuntimeError('Could not create native Opus decoder')
        self.output = (C.c_int16*5760)()
        self.previous_end = None
        self.metrics = metrics if metrics is not None else {}
        self.metrics.update(concealed_packets=0,skipped_long_or_irregular_loss=0)

    def close(self):
        if self.pointer:
            self.lib.opus_decoder_destroy(self.pointer)
            self.pointer = None

    def _frame(self, payload, timestamp, *, frame_size=5760, fec=0):
        size = self.lib.opus_decode(self.pointer,payload,len(payload) if payload else 0,
                                   self.output,frame_size,fec)
        if size < 0:
            raise RuntimeError('Native Opus decode failed: '+str(size))
        frame = AudioFrame(format='s16',layout='mono',samples=size)
        frame.planes[0].update(C.string_at(self.output,size*2))
        frame.sample_rate=48000; frame.time_base=Fraction(1,48000); frame.pts=timestamp
        return frame

    def decode(self, encoded):
        if getattr(encoded,'reset_decoder',False):
            self.lib.opus_decoder_init(self.pointer,48000,1)
            self.previous_end=None
        payload=encoded.data
        samples=self.lib.opus_packet_get_nb_samples(payload,len(payload),48000)
        missing=int(getattr(encoded,'missing_packets',0))
        timestamp=encoded.timestamp
        frames=[]
        # The current live path uses 20 ms Opus packets. Do not guess variable
        # packet durations, fill large gaps, or turn timestamp resets to speech.
        if (0<missing<=5 and samples==960 and self.previous_end is not None
                and timestamp-self.previous_end==missing*960):
            for index in range(missing):
                last=index==missing-1
                frames.append(self._frame(payload if last else None,
                    self.previous_end+index*960,frame_size=960,fec=int(last)))
            self.metrics['concealed_packets']+=missing
        elif missing:
            self.metrics['skipped_long_or_irregular_loss']+=missing
            self.lib.opus_decoder_init(self.pointer,48000,1)
        frame=self._frame(payload,timestamp)
        frames.append(frame)
        self.previous_end=timestamp+frame.samples
        return frames


def install_recovery_decoder(receiver, on_failure, metrics):
    """Replace one instance's pinned decoder startup, not aiortc globals."""
    if aiortc.__version__ != '1.15.0':
        raise RuntimeError('Opus receive adapter requires tested aiortc 1.15.0')
    opus_library()  # Fail during preparation, never half way into a call.
    required = ('__started','__codecs','__rtx_ssrc','__decoder_queue','__transport')
    if not all(hasattr(receiver,'_RTCRtpReceiver'+name) for name in required):
        raise RuntimeError('Unsupported aiortc receive interface')

    def worker(loop,input_q,output_q):
        codec_name=None; decoder=None
        try:
            while True:
                task=input_q.get()
                if task is None:
                    break
                codec,encoded=task
                if codec.name != codec_name:
                    if isinstance(decoder,OpusRecoveryDecoder): decoder.close()
                    decoder=OpusRecoveryDecoder(metrics) if codec.name.casefold()=='opus' else get_decoder(codec)
                    codec_name=codec.name
                for frame in decoder.decode(encoded):
                    asyncio.run_coroutine_threadsafe(output_q.put(frame),loop)
        except Exception as exc:
            loop.call_soon_threadsafe(on_failure,'Opus decoder failed: '+str(exc))
        finally:
            if isinstance(decoder,OpusRecoveryDecoder): decoder.close()
            asyncio.run_coroutine_threadsafe(output_q.put(None),loop)

    async def receive(instance,parameters):
        if instance._RTCRtpReceiver__started:
            return
        for codec in parameters.codecs:
            instance._RTCRtpReceiver__codecs[codec.payloadType]=codec
        for encoding in parameters.encodings:
            if encoding.rtx:
                instance._RTCRtpReceiver__rtx_ssrc[encoding.rtx.ssrc]=encoding.ssrc
        loop=asyncio.get_running_loop()
        thread=threading.Thread(target=worker,name='phone-opus-decoder',
            args=(loop,instance._RTCRtpReceiver__decoder_queue,instance._track._queue))
        instance._RTCRtpReceiver__decoder_thread=thread
        thread.start()
        instance._RTCRtpReceiver__transport._register_rtp_receiver(instance,parameters)
        instance._RTCRtpReceiver__rtcp_task=asyncio.ensure_future(instance._run_rtcp())
        instance._RTCRtpReceiver__started=True

    receiver.receive=MethodType(receive,receiver)
