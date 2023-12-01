import re
from core.voice_cloner import VoiceCloner
from core.dereverb import MDXNetDereverb
from core.scene_preprocessor import ScenePreprocessor
from core.face.lipsync import LipSync
from core.helpers import (
    to_segments, 
    to_extended_frames, 
    to_avi, 
    merge, 
    merge_voices, 
    find_speaker, 
    get_voice_segments
)
from core.translator import TextHelper
from core.audio import speedup_audio, combine_audio
from core.temp_manager import TempFileManager
from pydub import AudioSegment
from core.whisperx.asr import load_model, load_audio
from core.whisperx.alignment import load_align_model, align
from core.whisperx.diarize import DiarizationPipeline, assign_word_speakers
import torch
from itertools import groupby
import torch
import numpy as np
import subprocess
from pathlib import Path
from tqdm import tqdm
from moviepy.video.io.VideoFileClip import VideoFileClip

class Engine:
    def __init__(self, config, output_language):
        #if not config['HF_TOKEN']:
           # raise Exception('No HuggingFace token providen!')
        self.output_language = output_language
        print("output_language:{}".format(output_language))
        self.config = config
        device_type = 'cuda' if torch.cuda.is_available() else 'cpu'
        self.device = torch.device(device_type)
        self.whisper_batch_size = 16
        self.whisper = load_model('large-v2', device=device_type, compute_type='int8')
        self.diarize_model = DiarizationPipeline(use_auth_token=config['HF_TOKEN'])
        self.text_helper = TextHelper(config)
        self.temp_manager = TempFileManager()
        self.speaker_num = config["SPEAKER_NUM"]
        
        if self.speaker_num > 1: 
            self.scene_processor = ScenePreprocessor(config)
            self.lip_sync = LipSync()
            print("You can change SPEAKER_NUM as 1 in config.json to disable scene_processor")
        else:
            print("default speaker num is 1, you can change SPEAKER_NUM in config.json to enable scene_processor")
        
        self.dereverb = MDXNetDereverb(15)
    
    def __call__(self, video_file_path, output_file_path):
        # [Step 1] Reading the video, getting audio (voice + noise), as well as the text of the voice -------
        print("[Step 1] Reading the video, getting audio (voice + noise), as well as the text of the voice")
        orig_clip = VideoFileClip(video_file_path, verbose=False)
        original_audio_file = self.temp_manager.create_temp_file(suffix='.wav').name
        orig_clip.audio.write_audiofile(original_audio_file, codec='pcm_s16le', verbose=False, logger=None)

        dereverb_out = self.dereverb.split(original_audio_file)
        voice_audio = AudioSegment.from_file(dereverb_out['voice_file'], format='wav')
        noise_audio = AudioSegment.from_file(dereverb_out['noise_file'], format='wav')

        speakers, lang = self.transcribe_audio_extended(dereverb_out['voice_file'])
        # ---------------------------------------------------------------------------------------------------
        
        if self.speaker_num > 1: 
            # [Step 2] Getting voice segments, frames, do face detection + reidentification ---------------------

            print("[Step 2] Getting voice segments, frames, do face detection + reidentification")
            voice_segments = get_voice_segments(speakers)
            self.scene_processor(orig_clip, video_file_path, voice_segments)
            # ---------------------------------------------------------------------------------------------------

            # [Step 3] Trying to connect the voices and the detected people -------------------------------------
            print("[Step 3] Trying to connect the voices and the detected people")
            speaker_groups = groupby(speakers, key=lambda x: x['speaker'])
            connections = dict()

            for speaker_name, group in speaker_groups:
                connections[speaker_name] = []
                for speech_element in group:
                    speech_start_frame = int(speech_element['start'] * orig_clip.fps)
                    speech_end_frame = int(speech_element['end'] * orig_clip.fps)

                    for speech_frame_id in range(speech_start_frame, speech_end_frame + 1):
                        person_ids = self.scene_processor.get_persons_on_frame(speech_frame_id)
                        for person_id in person_ids:
                            connections[speaker_name].append(person_id)

            for speaker_name, groups in connections.items():
                speaker_id = find_speaker(groups)
                for speaker in speakers:
                    if speaker['speaker'] == speaker_name:
                        speaker['id'] = speaker_id
            # ---------------------------------------------------------------------------------------------------
        else:
            print("When speaker num is 1, step 2,3 skipped!!!")

        # [Step 4] Merging voices, translating speech, cloning voices ---------------------------------------
        print("[Step 4] Merging voices, translating speech, cloning voices")
        merged_voices = merge_voices(speakers, voice_audio)

        updates = []
        zimu_path = Path(output_file_path).parent.joinpath('zimu.txt')
        print(zimu_path)
        self.empty_cache()
        cloner = VoiceCloner(self.config,self.output_language)
        for speaker in speakers:
            if 'id' in speaker:
                voice = merged_voices[speaker['id']]
            else:
                voice = voice_audio[speaker['start'] * 1000: speaker['end'] * 1000]
            
            voice_wav = self.temp_manager.create_temp_file(suffix='.wav').name
            voice.export(voice_wav, format='wav')
            
            voice_audio_wav = self.temp_manager.create_temp_file(suffix='.wav').name
            voice_audio.export(voice_audio_wav, format='wav')

            dst_text = self.text_helper.translate(speaker['text'], src_lang=lang, dst_lang=self.output_language)
            
            with open(zimu_path, 'a', encoding="utf-8") as f:
                f.write("\n" + speaker['text'])
                f.write("\n" + dst_text)
                f.write("\n")
                
            cloned_wav = cloner.process(
                speaker_wav_filename=[voice_wav,voice_audio_wav],
                text=dst_text
            )

            sub_voice = voice_audio[speaker['start'] * 1000: speaker['end'] * 1000]
            sub_voice_wav = self.temp_manager.create_temp_file(suffix='.wav').name
            sub_voice.export(sub_voice_wav, format='wav')

            output_wav = speedup_audio(cloned_wav, sub_voice_wav)

            updates.append({
                # In ms
                'start': speaker['start'] * 1000,
                'end': speaker['end'] * 1000,
                'voice': output_wav
            })
        # ---------------------------------------------------------------------------------------------------
        cloner = None
        torch.cuda.empty_cache()
        # [Step 5] Creating final speech audio --------------------------------------------------------------
        print("[Step 5] Creating final speech audio")
        original_audio_duration = voice_audio.duration_seconds * 1000
        
        segments = to_segments(updates, original_audio_duration)

        speech_audio = AudioSegment.silent(duration=0)
        for segment in segments:
            if segment['empty']:
                duration = segment['end'] - segment['start']
                speech_audio += AudioSegment.silent(duration=duration)
            else:
                speech_audio += AudioSegment.from_file(segment['voice'])
        
        speech_audio_wav = self.temp_manager.create_temp_file(suffix='.wav').name
        speech_audio.export(speech_audio_wav, format='wav')
        # ---------------------------------------------------------------------------------------------------
        if self.speaker_num > 1: 
            # [Step 6] LipSync + merging frames -----------------------------------------------------------------
            print("[Step 6] LipSync + merging frames ")
            frames = dict()

            all_frames = self.scene_processor.get_frames()
            for frame_id, frame in all_frames.items():
                if not frame_id in frames:
                    frames[frame_id] = {
                        'frame': np.array(frame)
                    }

            frames = to_extended_frames(frames, speakers, orig_clip.fps, self.scene_processor.get_face_on_frame)
            self.scene_processor.close()
            frames = self.lip_sync.sync(frames, speech_audio_wav, orig_clip.fps)
            # ---------------------------------------------------------------------------------------------------

            # [Step 7] Merging speech voice and noise, creating output ------------------------------------------
            print("[Step 7] Merging speech voice and noise, creating output")
            temp_result_avi = to_avi(frames, orig_clip.fps)

            noise_audio_wav = self.temp_manager.create_temp_file(suffix='.wav').name
            noise_audio.export(noise_audio_wav, format='wav')

            combined_audio = combine_audio(speech_audio_wav, noise_audio_wav)

            merge(combined_audio, temp_result_avi, output_file_path)
        # ---------------------------------------------------------------------------------------------------
        else:
            # [Step 6] Using video-retalking merge speech voice and video, creating output ------------------------------------------
            print("Video-retalking merge speech voice and video, creating output!!!")
            noise_audio_wav = self.temp_manager.create_temp_file(suffix='.wav').name
            noise_audio.export(noise_audio_wav, format='wav')

            combined_audio = combine_audio(speech_audio_wav, noise_audio_wav)

            command = 'cd ./video-retalking && rm -rf ./temp/* && python inference.py \
            --face {} --audio {} --outfile {} --LNet_batch_size {}'.format(
                video_file_path, combined_audio, output_file_path, 2
            )
            subprocess.call(command, shell=True)
        
    
    def empty_cache(self):
        self.whisper = None
        self.diarize_model = None
        self.dereverb = None
        torch.cuda.empty_cache()
        print("cuda memeroy:{}".format(torch.cuda.memory_reserved()))
        print("You may need CTRL+C here!")
        
    def transcribe_audio_extended(self, audio_file):
        audio = load_audio(audio_file)
        result = self.whisper.transcribe(audio, batch_size=self.whisper_batch_size)
        language = result['language']
        model_a, metadata = load_align_model(language_code=language, device=self.device)
        result = align(result['segments'], model_a, metadata, audio, self.device, return_char_alignments=False)
        print("diarizing ... wait moment")
        diarize_segments = self.diarize_model(audio)
        result = assign_word_speakers(diarize_segments, result)
        return result['segments'], language