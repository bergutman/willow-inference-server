# Profiling
import cProfile as profile
import pstats
# Logging
import os
import sys
import logging
logger = logging.getLogger("infer")
gunicorn_logger = logging.getLogger('gunicorn.error')
logger.handlers = gunicorn_logger.handlers
logger.setLevel(gunicorn_logger.level)

# Tell the user we're starting ASAP
logger.info("AIR Infer API is starting... Please wait.")

# FastAPI preprocessor
from fastapi import FastAPI, UploadFile, Request, Response, status
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from typing import Optional, Tuple
from pydantic import BaseModel
import types
import random
import datetime
import numpy as np
import warnings
warnings.simplefilter(action='ignore')
import io
import re
import math
import functools
from typing import NamedTuple
from settings import get_api_settings
settings = get_api_settings()

# WebRTC
import asyncio
#import uvloop
#asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())
import uuid

from aiortc import RTCPeerConnection, RTCSessionDescription, RTCRtpReceiver
from aiortc.rtp import RtcpByePacket
from aia.media import MediaRecorderLite

pcs = set()

# Whisper
import ctranslate2
import librosa
import transformers
import datetime

# TTS
import soundfile as sf
from speechbrain.pretrained import EncoderClassifier
import torchaudio
# Use soundfile so we can support WAV, FLAC, etc
torchaudio.set_audio_backend('soundfile')
import tempfile
import shutil

sys.path.append("deps/toucan")
from aia.toucanTTSInferface import ToucanTTSInterface
toucan_model_id = "Meta"

import torch
# Import audio stuff adapted from ref Whisper implementation
from aia.audio import log_mel_spectrogram, pad_or_trim, chunk_iter, find_longest_common_sequence

# Sallow
import wave

# Function to create a wav file from stream data
def write_stream_wav(data, rates, bits, ch):
    t = datetime.datetime.utcnow()
    time = t.strftime('%Y%m%dT%H%M%SZ')
    #filename = str.format('audio/{}_{}_{}_{}.wav', time, rates, bits, ch)
    filename = 'audio/sallow.wav'
    wavfile = wave.open(filename, 'wb')
    wavfile.setparams((ch, int(bits/8), rates, 0, 'NONE', 'NONE'))
    wavfile.writeframesraw(bytearray(data))
    wavfile.close()
    return filename

# Monkey patch aiortc
# sender.replaceTrack(null) sends a RtcpByePacket which we want to ignore
# in this case and keep connection open. XXX: Are there other cases we want to close?
old_handle_rtcp_packet = RTCRtpReceiver._handle_rtcp_packet
async def new_handle_rtcp_packet(self, packet):
    if isinstance(packet, RtcpByePacket):
        return
    return old_handle_rtcp_packet(self, packet)
RTCRtpReceiver._handle_rtcp_packet = new_handle_rtcp_packet
#logging.basicConfig(level=logger.DEBUG) #very useful debugging aiortc issues

# Monkey patch aiortc to control ephemeral ports
local_ports = list(range(10000, 10000+300)) # Allowed ephemeral port range
loop = asyncio.get_event_loop()
old_create_datagram_endpoint = loop.create_datagram_endpoint
async def create_datagram_endpoint(self, protocol_factory,
    local_addr: Tuple[str, int] = None,
    **kwargs,
):
    #if port is specified just use it
    if local_addr and local_addr[1]:
        return await old_create_datagram_endpoint(protocol_factory, local_addr=local_addr, **kwargs)
    if local_addr is None: 
        return await old_create_datagram_endpoint(protocol_factory, local_addr=None, **kwargs)
    #if port is not specified make it use our range
    ports = list(local_ports)
    random.shuffle(ports)
    for port in ports:
        try:
            ret = await old_create_datagram_endpoint(
                protocol_factory, local_addr=(local_addr[0], port), **kwargs
            )
            logger.debug(f'create_datagram_endpoint chose port {port}')
            return ret
        except OSError as exc:
            if port == ports[-1]:
                # this was the last port, give up
                raise exc
    raise ValueError("local_ports must not be empty")
loop.create_datagram_endpoint = types.MethodType(create_datagram_endpoint, loop)

#XXX: rm these globals and use settings directly
# default return language
return_language = settings.return_language

# default beam_size - 5 is lib default, 1 for greedy
beam_size = settings.beam_size

# default beam size for longer transcriptions
long_beam_size = settings.long_beam_size
# Audio duration in ms to activate "long" mode
long_beam_size_threshold = settings.long_beam_size_threshold

# model threads
model_threads = settings.model_threads

# Support for chunking
support_chunking = settings.support_chunking

whisper_model_default = settings.whisper_model_default

tts_default_speaker = settings.tts_default_speaker

# Default detect language?
detect_language = settings.detect_language

# Try CUDA
device = "cuda" if torch.cuda.is_available() else "cpu"

if device == "cuda":
    cuda_num_devices = torch.cuda.device_count()
    logger.info(f'CUDA: Detected {cuda_num_devices} device(s)')

    # Assume Turing or higher
    compute_type = "int8_float16"

    for cuda_dev_num in range(0, cuda_num_devices, 1):
        # Print CUDA device name
        cuda_device_name = torch.cuda.get_device_name(cuda_dev_num)
        logger.info(f'CUDA: Device {cuda_dev_num} name: {cuda_device_name}')

        # Get CUDA device capability
        cuda_device_capability = torch.cuda.get_device_capability(cuda_dev_num)
        cuda_device_capability = functools.reduce(lambda sub, ele: sub * 10 + ele, cuda_device_capability)
        logger.info(f'CUDA: Device {cuda_dev_num} capability: {cuda_device_capability}')

        # Get CUDA memory - returns in bytes
        cuda_total_memory = torch.cuda.mem_get_info(cuda_dev_num)[1]
        cuda_free_memory = torch.cuda.mem_get_info(cuda_dev_num)[0]
        logger.info(f'CUDA: Device {cuda_dev_num} total memory: {cuda_total_memory} bytes')
        logger.info(f'CUDA: Device {cuda_dev_num} free memory: {cuda_free_memory} bytes')

        # Disable chunking if card has less than 10GB VRAM (complete guess)
        # This can still encounter out of memory errors depending on audio length
        if cuda_free_memory <= 10000000000:
            logger.warning(f'CUDA: Device {cuda_dev_num} has low memory, disabling chunking support')
            support_chunking = False

        # Override compute_type if at least one non-Turing card
        if cuda_device_capability <= 70:
            logger.warning(f'CUDA: Device {cuda_dev_num} is pre-Turing, forcing int8')
            compute_type = "int8"

    # Set ctranslate device index based on number of supported devices
    device_index = [*range(0, cuda_num_devices, 1)]
else:
    num_cpu_cores = os.cpu_count()
    compute_type = "int8"
    # Just kind of made these numbers up - needs testing
    intra_threads = num_cpu_cores // 2
    model_threads = num_cpu_cores // 2
    logger.info(f'CUDA: Not found - using CPU with {num_cpu_cores} cores')


class Models(NamedTuple):
    whisper_processor: any
    whisper_model_base: any
    whisper_model_medium: any
    whisper_model_large: any

    tts_processor: any
    tts_model: any
    tts_vocoder: any
    toucan_tts: any

models:Models = None

def load_models() -> Models:
    global models
    # Turn up log_level for ctranslate2
    #ctranslate2.set_log_level(logger.DEBUG)
    # Load processor from transformers
    whisper_processor = transformers.WhisperProcessor.from_pretrained("./models/openai-whisper-base")
    # Show supported compute types
    supported_compute_types = str(ctranslate2.get_supported_compute_types(device))
    logger.info(f'CTRANSLATE: Supported compute types for device {device} are {supported_compute_types} - using configured {compute_type}')

    # Load all models - thanks for quantization ctranslate2
    logger.info("Loading Whisper models...")
    if device == "cuda":
        whisper_model_base = ctranslate2.models.Whisper('models/openai-whisper-base', device=device, compute_type=compute_type, device_index=device_index, inter_threads=model_threads)
        whisper_model_medium = ctranslate2.models.Whisper('models/openai-whisper-medium', device=device, compute_type=compute_type, device_index=device_index, inter_threads=model_threads)
        whisper_model_large = ctranslate2.models.Whisper('models/openai-whisper-large-v2', device=device, compute_type=compute_type, device_index=device_index, inter_threads=model_threads)
    else:
        whisper_model_base = ctranslate2.models.Whisper('models/openai-whisper-base', device=device, compute_type=compute_type, inter_threads=model_threads, intra_threads=intra_threads)
        whisper_model_medium = ctranslate2.models.Whisper('models/openai-whisper-medium', device=device, compute_type=compute_type, inter_threads=model_threads, intra_threads=intra_threads)
        whisper_model_large = ctranslate2.models.Whisper('models/openai-whisper-large-v2', device=device, compute_type=compute_type, inter_threads=model_threads, intra_threads=intra_threads)

    logger.info("Loading TTS models...")
    tts_processor = transformers.SpeechT5Processor.from_pretrained("./models/microsoft-speecht5_tts")
    tts_model = transformers.SpeechT5ForTextToSpeech.from_pretrained("./models/microsoft-speecht5_tts").to(device=device)
    tts_vocoder = transformers.SpeechT5HifiGan.from_pretrained("./models/microsoft-speecht5_hifigan").to(device=device)
    toucan_tts = ToucanTTSInterface(device=device, tts_model_path=toucan_model_id, faster_vocoder=False)
    models = Models(whisper_processor, whisper_model_base, whisper_model_medium, whisper_model_large, tts_processor, tts_model, tts_vocoder, toucan_tts)
    return models

def warm_models():
    logger.info("Warming models...")
    for x in range(3):
        if models.whisper_model_base is not None:
            do_whisper("client/3sec.flac", "base", beam_size, "transcribe", False, "en")
        if models.whisper_model_medium is not None:
            do_whisper("client/3sec.flac", "medium", beam_size, "transcribe", False, "en")
        if models.whisper_model_large is not None:
            do_whisper("client/3sec.flac", "large", beam_size, "transcribe", False, "en")

def do_translate(features, language, beam_size=beam_size):
    # Set task in token format for processor
    task = 'translate'
    processor_task = f'<|{task}|>'

    # Describe the task in the prompt.
    # See the prompt format in https://github.com/openai/whisper.
    prompt = models.whisper_processor.tokenizer.convert_tokens_to_ids(
        [
            "<|startoftranscript|>",
            language,
            processor_task,
            "<|notimestamps|>",  # Remove this token to generate timestamps.
        ]
    )

    # Run generation for the 30-second window.
    time_start = datetime.datetime.now()
    results = models.whisper_model_large.generate(features, [prompt], beam_size=beam_size)
    time_end = datetime.datetime.now()
    infer_time = time_end - time_start
    infer_time_milliseconds = infer_time.total_seconds() * 1000
    logger.debug('WHISPER: Translate inference took ' + str(infer_time_milliseconds) + ' ms')
    results = models.whisper_processor.decode(results[0].sequences_ids[0])
    logger.debug(results)

    return results

def do_whisper(audio_file, model:str, beam_size:int, task:str, detect_language:bool, return_language:str):
    if model != "large" and detect_language == True:
        logger.warning(f'WHISPER: Language detection requested but not supported on model {model} - overriding with large')
        model = "large"
        beam_size = 5
    # Point to model object depending on passed model string
    if model == "large":
        whisper_model = models.whisper_model_large
    elif model == "medium":
        whisper_model = models.whisper_model_medium
    elif model == "base":
        whisper_model = models.whisper_model_base

    processor_task = f'<|{task}|>'
    first_time_start = datetime.datetime.now()

    # Whisper STEP 1 - load audio and extract features
    audio, audio_sr = librosa.load(audio_file, sr=16000, mono=True)
    audio_duration = librosa.get_duration(y=audio, sr=audio_sr) * 1000
    audio_duration = int(audio_duration)
    if audio_duration >= long_beam_size_threshold:
        logger.debug(f'WHISPER: Audio duration is {audio_duration} ms - activating long mode')
        beam_size = long_beam_size
    use_chunking = False
    if audio_duration > 30*1000:
        if support_chunking:
            logger.debug(f'WHISPER: Audio duration is > 30s - activating chunking')
            use_chunking = True
        else:
            logger.warning(f'WHISPER: Audio duration is > 30s but chunking is not available. Will truncate!')

    time_end = datetime.datetime.now()
    infer_time = time_end - first_time_start
    infer_time_milliseconds = infer_time.total_seconds() * 1000
    logger.debug('WHISPER: Loading audio took ' + str(infer_time_milliseconds) + ' ms')

    time_start = datetime.datetime.now()
    if use_chunking:
        chunks = []
        strides = []
        for chunk, stride in chunk_iter(audio):
            chunk = pad_or_trim(chunk)
            chunks.append(log_mel_spectrogram(chunk).numpy())
            strides.append(stride)
        mel_features = np.stack(chunks)
        batch_size = len(chunks)
    else:
        mel_audio = pad_or_trim(audio)
        mel_features = log_mel_spectrogram(mel_audio).numpy()
        # Ref Whisper returns shape (80, 3000) but model expects (1, 80, 3000)
        mel_features = np.expand_dims(mel_features, axis=0)
        batch_size = 1
    features = ctranslate2.StorageView.from_array(mel_features)

    time_end = datetime.datetime.now()
    infer_time = time_end - time_start
    infer_time_milliseconds = infer_time.total_seconds() * 1000
    logger.debug('WHISPER: Feature extraction took ' + str(infer_time_milliseconds) + ' ms')

    # Whisper STEP 2 - optionally actually detect the language or default to en
    time_start = datetime.datetime.now()
    if detect_language:
        results = whisper_model.detect_language(features)
        language, probability = results[0][0]
        logger.debug("WHISPER: Detected language %s with probability %f" % (language, probability))

    else:
        logger.debug('WHISPER: Hardcoding language to en')
        # Hardcode language
        language = '<|en|>'

    # Describe the task in the prompt.
    # See the prompt format in https://github.com/openai/whisper.
    prompt = models.whisper_processor.tokenizer.convert_tokens_to_ids(
        [
            "<|startoftranscript|>",
            language,
            processor_task,
            "<|notimestamps|>",  # Remove this token to generate timestamps.
        ]
    )
    time_end = datetime.datetime.now()
    infer_time = time_end - time_start
    infer_time_milliseconds = infer_time.total_seconds() * 1000
    logger.debug('WHISPER: Language detection took ' + str(infer_time_milliseconds) + ' ms')

    # Whisper STEP 3 - run model
    time_start = datetime.datetime.now()
    logger.debug(f'WHISPER: Using model {model} with beam size {beam_size}')
    results = whisper_model.generate(features, [prompt]*batch_size, beam_size=beam_size, return_scores=False)
    time_end = datetime.datetime.now()
    infer_time = time_end - time_start
    infer_time_milliseconds = infer_time.total_seconds() * 1000
    logger.debug('WHISPER: Model took ' + str(infer_time_milliseconds) + ' ms')
    
    time_start = datetime.datetime.now()
    if use_chunking:
        assert strides, 'strides needed to compute final tokens when chunking'
        tokens = [(results[i].sequences_ids[0], strides[i]) for i in range(batch_size)]
        tokens = find_longest_common_sequence(tokens, models.whisper_processor.tokenizer)
    else:
        tokens = results[0].sequences_ids[0]
    results = models.whisper_processor.decode(tokens)
    time_end = datetime.datetime.now()
    infer_time = time_end - time_start
    infer_time_milliseconds = infer_time.total_seconds() * 1000
    logger.debug('WHISPER: Decode took ' + str(infer_time_milliseconds) + ' ms')
    logger.debug('WHISPER: ASR transcript: ' + results)

    # Strip out token stuff
    pattern = re.compile("[A-Za-z0-9]+", )
    language = pattern.findall(language)[0]

    if not language == return_language:
        logger.debug(f'WHISPER: Detected non-preferred language {language}, translating to {return_language}')
        translation = do_translate(features, language, beam_size=beam_size)
        # Strip tokens from translation output - brittle but works right now
        translation = translation.split('>')[2]
        translation = translation.strip()
        logger.debug('WHISPER: ASR translation: {translation}')
    else:
        translation = None

    # Remove trailing and leading spaces
    results = results.strip()

    time_end = datetime.datetime.now()
    infer_time = time_end - first_time_start
    infer_time_milliseconds = infer_time.total_seconds() * 1000
    logger.debug('WHISPER: Inference took ' + str(infer_time_milliseconds) + ' ms')
    infer_speedup = math.floor(audio_duration / infer_time_milliseconds)
    logger.debug('WHISPER: Inference speedup: ' + str(infer_speedup) + 'x')

    return language, results, infer_time_milliseconds, translation, infer_speedup, audio_duration


def do_tts(text, format, speaker = tts_default_speaker):
    logger.debug(f'TTS: Got request for speaker {speaker} with text: {text}')

    # Load speaker embedding
    time_initial_start = datetime.datetime.now()

    file_path = f"aia/assets/spkemb/{speaker}.npy"
    if os.path.isfile(file_path):
        speaker_numpy = file_path
        logger.debug(f'TTS: Loaded included speaker {speaker}')

    # Try and potentially override with a custom speaker
    file_path = f"custom_speakers/{speaker}.npy"
    if os.path.isfile(file_path):
        speaker_numpy = file_path
        logger.debug(f'TTS: Loaded custom speaker {speaker}')

    speaker_embedding = np.load(speaker_numpy)
    speaker_embedding = torch.tensor(speaker_embedding).unsqueeze(0).to(device=device)
    time_end = datetime.datetime.now()
    infer_time = time_end - time_initial_start
    infer_time_milliseconds = infer_time.total_seconds() * 1000
    logger.debug('TTS: Loading speaker embedding took ' + str(infer_time_milliseconds) + ' ms')

    # Get inputs
    time_start = datetime.datetime.now()
    inputs = models.tts_processor(text=text, return_tensors="pt").to(device=device)
    time_end = datetime.datetime.now()
    infer_time = time_end - time_start
    infer_time_milliseconds = infer_time.total_seconds() * 1000
    logger.debug('TTS: Getting inputs took ' + str(infer_time_milliseconds) + ' ms')

    # Generate spectrogram - SLOW
    time_start = datetime.datetime.now()
    spectrogram = models.tts_model.generate_speech(inputs["input_ids"], speaker_embedding).to(device=device)
    time_end = datetime.datetime.now()
    infer_time = time_end - time_start
    infer_time_milliseconds = infer_time.total_seconds() * 1000
    logger.debug('TTS: Generating spectrogram took ' + str(infer_time_milliseconds) + ' ms')
    
    # Generate audio - SLOW
    time_start = datetime.datetime.now()
    audio = models.tts_model.generate_speech(inputs["input_ids"], speaker_embedding, vocoder=models.tts_vocoder).to(device=device)
    time_end = datetime.datetime.now()
    infer_time = time_end - time_start
    infer_time_milliseconds = infer_time.total_seconds() * 1000
    logger.debug('TTS: Generating audio took ' + str(infer_time_milliseconds) + ' ms')

    # Setup access to file and pass it back to calling function
    time_start = datetime.datetime.now()

    # If we're not running on CPU copy audio to CPU so we can write it out
    if device != "cpu":
        audio = audio.cpu()

    file = io.BytesIO()
    sf.write(file, audio.numpy(), samplerate=16000, format=format)
    file.seek(0)
    time_end = datetime.datetime.now()
    infer_time = time_end - time_start
    infer_time_milliseconds = infer_time.total_seconds() * 1000
    logger.debug('TTS: Generating file took ' + str(infer_time_milliseconds) + ' ms')

    time_end = datetime.datetime.now()
    infer_time = time_end - time_initial_start
    infer_time_milliseconds = infer_time.total_seconds() * 1000
    logger.debug('TTS: Total time took ' + str(infer_time_milliseconds) + ' ms')

    return file

# Adapted from https://github.com/thingless/t5voice
def do_speaker_embed(audio_file, speaker_name):
    spk_model = "speechbrain/spkrec-xvect-voxceleb"
    size_embed = 512
    # Override to CPU for now
    device = "cpu"

    tmpdir = tempfile.mkdtemp()
    try:
        classifier = EncoderClassifier.from_hparams(source=spk_model, run_opts={"device": device}, savedir=tmpdir)

        audio, sr = torchaudio.load(audio_file)
        if len(audio.shape) > 1 and audio.shape[0] > 1:
            audio = audio[0]  # left channel only
        if sr != 16000:
            # resample
            resampler = torchaudio.transforms.Resample(sr, 16000, dtype=audio.dtype)
            audio = resampler(audio)
        with torch.no_grad():
            embeddings = classifier.encode_batch(audio)
            embeddings = torch.nn.functional.normalize(embeddings, dim=2)
            save_path = f"custom_speakers/{speaker_name}"
            np.save(save_path, embeddings.squeeze())
            embeddings = embeddings.squeeze().cpu().numpy()
        assert embeddings.shape[0] == size_embed, embeddings.shape[0]
    finally:
        assert len(tmpdir) > 3
        shutil.rmtree(tmpdir)

    return embeddings, save_path

# Function for WebRTC handling
async def rtc_offer(request, model, beam_size, task, detect_language, return_language):
    params = await request.json()
    offer = RTCSessionDescription(sdp=params["sdp"], type=params["type"])

    pc = RTCPeerConnection()
    pcs.add(pc)

    recorder = None
    top_track = None

    logger.debug(f'RTC: Created for {request.client.host}')

    @pc.on("datachannel")
    def on_datachannel(channel):
        @channel.on("message")
        def on_message(message):
            logger.debug("RTC DC: message: " + message)
            if isinstance(message, str) and message.startswith("ping"):
                channel.send("pong" + message[4:])
            if isinstance(message, str) and message.startswith("start"):
                logger.debug("RTC DC: Recording started")
                nonlocal recorder
                # XXX what if top_track is still None i.e. we got start before we got track?
                logger.debug(f'RTC DC: Recording with track {top_track}')
                recorder = MediaRecorderLite()
                recorder.addTrack(top_track)
                recorder.start()
                channel.send('ASR Recording - start talking and press stop when done')
            if isinstance(message, str) and message.startswith("stop"):
                try:
                    action_list = message.split(":")
                    logger.debug(f'RTC DC: Got action list {action_list}')
                except:
                    logger.debug('RTC DC: Failed to get action list - setting to none')
                    action_list = None
                if action_list[1] is not None:
                    model = action_list[1]
                    logger.debug(f'RTC DC: Got DC provided model {model}')
                else:
                    logger.debug('RTC DC: Failed getting model from DC')
                if action_list[2] is not None:
                    beam_size = int(action_list[2])
                    logger.debug(f'RTC DC: Got DC provided beam size {beam_size}')
                else:
                    logger.debug('RTC DC: Failed getting beam size from DC')
                if action_list[3] is not None:
                    detect_language = eval(action_list[3])
                    logger.debug(f'RTC DC: Got DC provided detect language {detect_language}')
                else:
                    logger.debug('RTC DC: Failed getting detect language from DC')
                logger.debug(f'RTC DC: Debug vars {model} {beam_size} {detect_language}')
                logger.debug("RTC DC: Recording stopped")
                time_start_base = datetime.datetime.now()
                time_end = datetime.datetime.now()
                infer_time = time_end - time_start_base
                infer_time_milliseconds = infer_time.total_seconds() * 1000
                recorder.stop()
                logger.debug('RTC DC: Recorder stop took ' + str(infer_time_milliseconds) + ' ms')
                # Tell client what we are doing
                channel.send(f'Doing ASR with model {model} beam size {beam_size} detect language {detect_language} - please wait')
                # Finally call Whisper
                recorder.file.seek(0)
                language, results, infer_time, translation, infer_speedup, audio_duration = do_whisper(recorder.file, model, beam_size, task, detect_language, return_language)
                logger.debug("RTC DC: " + results)
                channel.send('ASR Transcript: ' + results)
                if translation:
                    channel.send(f'ASR Translation from {language}:  {translation}')
                infer_time = str(infer_time)
                audio_duration = str(audio_duration)
                infer_speedup = str(infer_speedup)
                channel.send(f'ASR Infer time: {infer_time} ms')
                channel.send(f'ASR Audio Duration: {audio_duration} ms')
                channel.send(f'ASR Speedup: {infer_speedup}x faster than realtime')

                #del recorder

    @pc.on("connectionstatechange")
    async def on_connectionstatechange():
        logger.debug(f'RTC: Connection state is {pc.connectionState}')
        if pc.connectionState == "failed" or pc.connectionState == "closed":
            try:
                await recorder.stop()
            except:
                pass
            else:
                logger.debug("RTC: Recording stopped")
            await pc.close()
            pcs.discard(pc)
            #XXX: close recorders?
            logger.debug("RTC: Connection ended")

    @pc.on("track")
    def on_track(track):
        logger.debug(f'RTC: Track received {track.kind}')
        if track.kind == "audio":
            logger.debug("RTC: Setting top track")
            nonlocal top_track
            top_track = track

        @track.on("ended")
        async def on_ended():
            logger.debug(f'RTC: Track ended {track.kind}')

    # handle offer
    await pc.setRemoteDescription(offer)

    # send answer
    answer = await pc.createAnswer()
    await pc.setLocalDescription(answer)

    return {"sdp": pc.localDescription.sdp, "type": pc.localDescription.type}

app = FastAPI(title="AIR Infer API",
    description="High performance speech API",
    version="0.0.1")

@app.on_event("startup")
def startup_event():
    load_models()
    warm_models()
    logger.info("AIR Infer API is ready for requests!")

@app.on_event("shutdown")
def shutdown_event():
    logger.info("AIR Infer API is stopping (this can take a while)...")

# BROKEN: Disable middleware for now
#@app.middleware("http")
async def do_auth(request: Request, response: Response, call_next):
    api_key = os.environ.get('API_KEY')
    auth_header = request.headers.get('X-API-Key')

    logger.debug(f'FASTAPI: Got API key {api_key}')
    logger.debug(f'FASTAPI: Got API key header {auth_header}')

    if api_key is not None:
        if auth_header == api_key:
            logger.debug(f'FASTAPI: Request is authorized')
        else:
            logger.debug(f'FASTAPI: Request is unauthorized')
            response =  {"result": "no_auth"}
            response.status_code = status.HTTP_401_UNAUTHORIZED
            return response
    else:
        # If we got here we either don't have auth or it's good
        response = await call_next(request)
        return response

# Mount static dir to serve files for aiortc client
app.mount("/rtc", StaticFiles(directory="rtc", html = True), name="rtc_files")

# Temporary hack for the sallow stuff
app.mount("/audio", StaticFiles(directory="audio", html = True), name="audio_files")

class Ping(BaseModel):
    message: str

@app.get("/ping", response_model=Ping, summary="Ping for connectivity check", response_description="pong")
async def ping():
    response = jsonable_encoder({"message": "pong"})
    return JSONResponse(content=response)

@app.post("/api/rtc/asr", summary="Return SDP for WebRTC clients", response_description="SDP for WebRTC clients")
async def rtc_asr(request: Request, response: Response, model: Optional[str] = whisper_model_default, task: Optional[str] = "transcribe", detect_language: Optional[bool] = detect_language, return_language: Optional[str] = return_language, beam_size: Optional[int] = beam_size):
    response = await rtc_offer(request, model, beam_size, task, detect_language, return_language)
    return JSONResponse(content=response)

class ASR(BaseModel):
    language: str
    results: str
    infer_time: float
    translation: Optional[str]
    infer_speedup: int
    audio_duration: int
    text: str

@app.post("/api/asr", response_model=ASR, summary="Submit audio file for ASR", response_description="ASR engine output")
async def asr(request: Request, audio_file: UploadFile, response: Response, model: Optional[str] = whisper_model_default, task: Optional[str] = "transcribe", detect_language: Optional[bool] = detect_language, return_language: Optional[str] = return_language, beam_size: Optional[int] = beam_size):
    #prof = profile.Profile()
    #prof.enable()

    logger.debug(f"FASTAPI: Got ASR request for model {model} beam size {beam_size} language detection {detect_language}")
    # Setup access to file
    audio_file = io.BytesIO(await audio_file.read())
    # Do Whisper
    language, results, infer_time, translation, infer_speedup, audio_duration = do_whisper(audio_file, model, beam_size, task, detect_language, return_language)

    # Create final response
    final_response = {"infer_time": infer_time, "infer_speedup": infer_speedup, "audio_duration": audio_duration, "language": language, "text": results}

    # Handle translation in one response
    if translation:
        final_response['translation']=translation

    json_compatible_item_data = jsonable_encoder(final_response)
    #prof.disable()
    # print profiling output
    #stats = pstats.Stats(prof).strip_dirs().sort_stats("cumtime")
    #stats.print_stats(10) # top 10 rows
    return JSONResponse(content=json_compatible_item_data)

@app.post("/api/sallow", summary="Stream audio for ASR", response_description="Output as text")
async def sallow(request: Request, response: Response, model: Optional[str] = whisper_model_default, task: Optional[str] = "transcribe", detect_language: Optional[bool] = True, return_language: Optional[str] = return_language, beam_size: Optional[int] = 5, speaker: Optional[str] = tts_default_speaker):
    logger.debug(f"FASTAPI: Got Sallow request for model {model} beam size {beam_size} language detection {detect_language}")

    # Set defaults
    sample_rates = 0
    bits = 0
    channel = 0

    body = b''
    sample_rates = request.headers.get('x-audio-sample-rate', '').lower()
    bits = request.headers.get('x-audio-bits', '').lower()
    channel = request.headers.get('x-audio-channel', '').lower()

    audio_info = ("SALLOW: Audio information, sample rate: {}, bits: {}, channel(s): {}".format(sample_rates, bits, channel))
    logger.debug(audio_info)

    async for chunk in request.stream():
        body += chunk

    audio_file = write_stream_wav(body, int(sample_rates), int(bits), int(channel))

    # Do Whisper
    language, results, infer_time, translation, infer_speedup, audio_duration = do_whisper(audio_file, model, beam_size, task, detect_language, return_language)

    # Create final response
    final_response = {"infer_time": infer_time, "infer_speedup": infer_speedup, "audio_duration": audio_duration, "language": language, "text": results}

    # Handle translation in one response
    if translation:
        final_response['translation']=translation
        results = translation

    json_compatible_item_data = jsonable_encoder(final_response)

    response = do_tts(results, 'WAV', speaker)
    with open("audio/sallow-tts.wav", "wb") as outfile:
        # Copy the BytesIO stream to the output file
        outfile.write(response.getbuffer())

    return results

@app.get("/api/tts", summary="Submit text for text to speech", response_description="Audio file of generated speech")
async def tts(text: str, speaker: Optional[str] = tts_default_speaker):
    logger.debug(f"FASTAPI: Got TTS request for speaker {speaker} and text: {text}")
    # Do TTS
    response = do_tts(text, 'FLAC', speaker)
    return StreamingResponse(response, media_type="audio/flac")

@app.get("/api/ttts", summary="Submit text for text to speech to toucan", response_description="Audio file of generated speech")
def ttts(text: str, speaker: Optional[str] = tts_default_speaker):
    logger.debug(f"FASTAPI: Got TTTS request for speaker {speaker} and text: {text}")
    # Do TTS
    models.toucan_tts.set_language("en")
    audio_file = io.BytesIO()
    response = models.toucan_tts.do_tts(text_list=[text])
    sf.write(file=audio_file, data=response, format='FLAC', samplerate=24000)
    response_file = audio_file.seek(0)
    return StreamingResponse(response_file, media_type="audio/flac")

@app.post("/api/sts", summary="Submit speech, do ASR, and TTS", response_description="Audio file of generated speech")
async def sts(request: Request, audio_file: UploadFile, response: Response, model: Optional[str] = whisper_model_default, task: Optional[str] = "transcribe", detect_language: Optional[bool] = detect_language, return_language: Optional[str] = return_language, beam_size: Optional[int] = beam_size, speaker: Optional[str] = tts_default_speaker):
    logger.debug(f"FASTAPI: Got STS request for model {model} beam size {beam_size} language detection {detect_language}")
    # Setup access to file
    audio_file = io.BytesIO(await audio_file.read())
    # Do Whisper
    language, results, infer_time, translation, infer_speedup, audio_duration = do_whisper(audio_file, model, beam_size, task, detect_language, return_language)

    # Do TTS
    response = do_tts(results, 'FLAC', speaker)
    return StreamingResponse(response, media_type="audio/flac")

class Speaker(BaseModel):
    message: str

@app.post("/api/speaker", response_model=Speaker, summary="Add custom speaker", response_description="Speaker creation status")
async def speaker_create(request: Request, audio_file: UploadFile, speaker_name: Optional[str] = "CUSTOM"):
    logger.debug(f"FASTAPI: Got new speaker request")
    # Setup access to file
    audio_file = io.BytesIO(await audio_file.read())
    # Do embed but don't do anything with the output other than save in do_speaker_embed
    embedding, save_path = do_speaker_embed(audio_file, speaker_name)
    status_text = f"Created custom speaker successfully - you can now use the {speaker_name} speaker for TTS"
    logger.debug(f"FASTAPI: {status_text}")

    response = jsonable_encoder({"message": status_text})
    return JSONResponse(content=response)

@app.delete("/api/speaker", response_model=Speaker, summary="Delete custom speaker", response_description="Speaker deletion status")
async def speaker_delete(request: Request, speaker_name: Optional[str] = "CUSTOM"):
    logger.debug(f"FASTAPI: Got delete speaker request")
    os.remove(f'custom_speakers/{speaker_name}.npy')
    status_text = f"Deleted custom speaker {speaker_name} successfully"
    logger.debug(f"FASTAPI: {status_text}")

    response = jsonable_encoder({"message": status_text})
    return JSONResponse(content=response)

class SpeakersList(BaseModel):
    speakers: list

@app.get("/api/speaker", response_model=SpeakersList, summary="Show supported speakers", response_description="Speakers list")
async def speaker_delete(request: Request):
    logger.debug(f"FASTAPI: Got list speakers request")
    speakers = []
    dirs = [ "aia/assets/spkemb", "custom_speakers" ]
    for dir in dirs:
        logger.debug(f"FASTAPI: Getting speakers for directory {dir}")
        for (root, dirs, file) in os.walk(dir):
            for f in file:
                if '.npy' in f:
                    name = f.replace(".npy", "")
                    logger.debug(f"FASTAPI: Getting speakers found speaker {name}")
                    speakers.append(name)

    response = jsonable_encoder({"speakers": speakers})
    return JSONResponse(content=response)