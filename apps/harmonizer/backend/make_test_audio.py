import numpy as np
import soundfile as sf

SR = 16000


def click_track(times, sr=SR, duration=20.0, click_len=0.02, freq=1000):
    n = int(duration * sr)
    audio = np.zeros(n)
    t = np.linspace(0, click_len, int(click_len * sr), endpoint=False)
    click = np.sin(2 * np.pi * freq * t) * np.exp(-t * 40)
    for time in times:
        start = int(time * sr)
        end = min(n, start + len(click))
        if start < n:
            audio[start:end] += click[: end - start]
    audio += np.random.normal(0, 0.002, n)
    return audio


rng = np.random.default_rng(0)
ref_beats = np.arange(0.5, 19.5, 0.5)
ref_audio = click_track(ref_beats)
sf.write("ref.wav", ref_audio, SR)

take1_beats = ref_beats * 1.0 + 0.3
take1_audio = click_track(take1_beats)
sf.write("take1.wav", take1_audio, SR)

take2_beats = ref_beats.copy() + 1.1
drift = np.linspace(0, 0.15, len(take2_beats))
take2_beats = take2_beats + drift
take2_audio = click_track(take2_beats)
sf.write("take2.wav", take2_audio, SR)

take3_beats = ref_beats.copy() * 1.03 + 2.0
take3_audio = click_track(take3_beats)
sf.write("take3.wav", take3_audio, SR)

print("wrote ref.wav, take1.wav, take2.wav, take3.wav")
