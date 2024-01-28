import tempfile
import uuid
from dataclasses import asdict
from dataclasses import dataclass
from pathlib import Path
from typing import Tuple

import argbind
import audiotools as at
import gradio as gr
import numpy as np
import yaml
import torch

from vampnet import mask as pmask
from vampnet.interface import Interface

interface = Interface(
    device="cuda" if torch.cuda.is_available() else "cpu",
    vampnet_ckpt="runs/spotdl-500m-jan19/latest/vampnet/weights.pth", 
    codec_ckpt="models/codec.pth",
)

# populate the model choices with any interface.yml files in the generated confs
MODEL_CHOICES = {
    "default": str(interface.ckpts["vampnet"]), 
}
generated_confs = Path("conf/generated")
for conf_file in generated_confs.glob("*/interface.yml"):
    with open(conf_file) as f:
        _conf = yaml.safe_load(f)
        MODEL_CHOICES[conf_file.parent.name] = _conf

    

OUT_DIR = Path("gradio-outputs")
OUT_DIR.mkdir(exist_ok=True, parents=True)


def load_audio(file):
    print(file)
    filepath = file.name
    # sig = at.AudioSignal.salient_excerpt(
    #     filepath, duration=interface.coarse.chunk_size_s
    # )
    # sig = interface.preprocess(sig)
    sig = at.AudioSignal(filepath)

    out_dir = OUT_DIR / "tmp" / str(uuid.uuid4())
    out_dir.mkdir(parents=True, exist_ok=True)
    sig.write(out_dir / "input.wav")
    return sig.path_to_file


def load_example_audio():
    return "./assets/example.wav"


def _vamp(data, return_mask=False):
    out_dir = OUT_DIR / str(uuid.uuid4())
    out_dir.mkdir()

    sig = at.AudioSignal(data[input_audio])
    sig = interface.preprocess(sig)

    loudness = sig.loudness()

    z = interface.encode(sig)

    print(f"reloading to {data[model_choice]}")
    interface.reload(MODEL_CHOICES[data[model_choice]])

    # build the mask
    mask = pmask.linear_random(z, data[rand_mask_intensity])
    mask = pmask.mask_and(
        mask,
        pmask.inpaint(z, interface.s2t(data[prefix_s]), interface.s2t(data[suffix_s])),
    )
    mask = pmask.mask_and(
        mask,
        pmask.periodic_mask(z, data[periodic_p], data[periodic_w], random_roll=True),
    )
    if data[onset_mask_width] > 0:
        mask = pmask.mask_or(
            mask, pmask.onset_mask(sig, z, interface, width=data[onset_mask_width])
        )
    if data[beat_mask_width] > 0:
        beat_mask = interface.make_beat_mask(
            sig,
            after_beat_s=(data[beat_mask_width]/1000), 
            mask_upbeats=not data[beat_mask_downbeats],
        )
        mask = pmask.mask_and(mask, beat_mask)

    # these should be the last two mask ops
    mask = pmask.dropout(mask, data[dropout])
    mask = pmask.codebook_mask(mask, int(data[n_mask_codebooks]))


    
    _top_p = data[top_p] if data[top_p] > 0 else None
    # save the mask as a txt file
    np.savetxt(out_dir / "mask.txt", mask[:, 0, :].long().cpu().numpy())

    _seed = data[seed] if data[seed] > 0 else None
    zv, mask_z = interface.vamp(
        z,
        mask=mask,
        # _sampling_steps=[data[num_steps], 8, 8, 4, 4, 2, 2, 1, 1],
        mask_temperature=data[masktemp]*10,
        sampling_temperature=data[sampletemp],
        return_mask=True, 
        typical_filtering=data[typical_filtering], 
        typical_mass=data[typical_mass], 
        typical_min_tokens=data[typical_min_tokens], 
        top_p=_top_p,
        seed=_seed,
        sample_cutoff=data[sample_cutoff],
    )


    sig = interface.to_signal(zv).cpu()
    print("done")

    sig = sig.normalize(loudness)
    sig.write(out_dir / "output.wav")

    if return_mask:
        mask = interface.to_signal(mask_z).cpu()
        mask.write(out_dir / "mask.wav")
        return sig.path_to_file, mask.path_to_file
    else:
        return sig.path_to_file



def vamp(data):
    return _vamp(data, return_mask=True)


def api_vamp(data):
    return _vamp(data, return_mask=False)


with gr.Blocks() as demo:
    with gr.Row():
        with gr.Column():
            gr.Markdown("# VampNet")
    with gr.Row():
        with gr.Column():

            manual_audio_upload = gr.File(
                label=f"upload some audio ",
                file_types=["audio"],
            )
            load_example_audio_button = gr.Button("or load example audio")

            input_audio = gr.Audio(
                label="input audio (will be ignored if unconditional)",
                interactive=False,
                type="filepath",
            )

            audio_mask = gr.Audio(
                label="audio mask (you should listen to this to hear the mask hints)",
                interactive=False,
                type="filepath",
            )

            # connect widgets
            load_example_audio_button.click(
                fn=load_example_audio,
                inputs=[],
                outputs=[ input_audio]
            )

            manual_audio_upload.change(
                fn=load_audio, inputs=[manual_audio_upload], outputs=[input_audio]
            )

        # mask settings
        with gr.Column():


            with gr.Accordion("manual controls", open=True):
                periodic_p = gr.Slider(
                    label="periodic prompt  (0 - unconditional, 2 - lots of hints, 8 - a couple of hints, 16 - occasional hint, 32 - very occasional hint, etc)",
                    minimum=0,
                    maximum=128, 
                    step=1,
                    value=3, 
                )


                onset_mask_width = gr.Slider(
                    label="onset mask width (multiplies with the periodic mask, 1 step ~= 10milliseconds) ",
                    minimum=0,
                    maximum=100,
                    step=1,
                    value=5,
                )

                beat_mask_width = gr.Slider(
                    label="beat mask width (in milliseconds)",
                    minimum=0,
                    maximum=200,
                    value=0,
                )
                beat_mask_downbeats = gr.Checkbox(
                    label="beat mask downbeats only?", 
                    value=False
                )

                n_mask_codebooks = gr.Number(
                    label="first upper codebook level to mask",
                    value=9,
                )

                with gr.Accordion("extras ", open=False):
                    pitch_shift_amt = gr.Slider(
                        label="pitch shift amount (semitones)",
                        minimum=-12,
                        maximum=12,
                        step=1,
                        value=0,
                    )

                    rand_mask_intensity = gr.Slider(
                        label="random mask intensity. (If this is less than 1, scatters prompts throughout the audio, should be between 0.9 and 1.0)",
                        minimum=0.0,
                        maximum=1.0,
                        value=1.0
                    )

                    periodic_w = gr.Slider(
                        label="periodic prompt width (steps, 1 step ~= 10milliseconds)",
                        minimum=1,
                        maximum=20,
                        step=1,
                        value=1,
                    )
                    n_conditioning_codebooks = gr.Number(
                        label="number of conditioning codebooks. probably 0", 
                        value=0,
                        precision=0,
                    )

                    stretch_factor = gr.Slider(
                        label="time stretch factor",
                        minimum=0,
                        maximum=64, 
                        step=1,
                        value=1, 
                    )

            with gr.Accordion("prefix/suffix prompts", open=False):
                prefix_s = gr.Slider(
                    label="prefix hint length (seconds)",
                    minimum=0.0,
                    maximum=10.0,
                    value=0.0,
                )
                suffix_s = gr.Slider(
                    label="suffix hint length (seconds)",
                    minimum=0.0,
                    maximum=10.0,
                    value=0.0,
                )

            masktemp = gr.Slider(
                label="mask temperature",
                minimum=0.0,
                maximum=100.0,
                value=1.5
            )
            sampletemp = gr.Slider(
                label="sample temperature",
                minimum=0.1,
                maximum=10.0,
                value=1.0, 
                step=0.001
            )


            with gr.Accordion("sampling settings", open=False):
                top_p = gr.Slider(
                    label="top p (0.0 = off)",
                    minimum=0.0,
                    maximum=1.0,
                    value=0.0
                )
                typical_filtering = gr.Checkbox(
                    label="typical filtering ",
                    value=False
                )
                typical_mass = gr.Slider( 
                    label="typical mass (should probably stay between 0.1 and 0.5)",
                    minimum=0.01,
                    maximum=0.99,
                    value=0.15,
                )
                typical_min_tokens = gr.Slider(
                    label="typical min tokens (should probably stay between 1 and 256)",
                    minimum=1,
                    maximum=256,
                    step=1,
                    value=64,
                )
                sample_cutoff = gr.Slider(
                    label="sample cutoff",
                    minimum=0.0,
                    maximum=1.0,
                    value=1.0, 
                    step=0.01
                )

            num_steps = gr.Slider(
                label="number of steps (should normally be between 12 and 36)",
                minimum=1,
                maximum=128,
                step=1,
                value=36,
            )

            dropout = gr.Slider(
                label="mask dropout", minimum=0.0, maximum=1.0, step=0.01, value=0.0
            )

            seed = gr.Number(
                label="seed (0 for random)",
                value=0,
                precision=0,
            )
        

        with gr.Column():
            model_choice = gr.Dropdown(
                label="model choice", 
                choices=list(MODEL_CHOICES.keys()),
                value="default", 
                visible=True
            )

            vamp_button = gr.Button("generate (vamp)!!!")
            output_audio = gr.Audio(
                label="output audio", interactive=False, type="filepath"
            )

            use_as_input_button = gr.Button("use output as input")


    _inputs = {
            input_audio, 
            num_steps,
            masktemp,
            sampletemp,
            top_p,
            prefix_s, suffix_s, 
            rand_mask_intensity, 
            periodic_p, periodic_w,
            n_conditioning_codebooks, 
            dropout,
            stretch_factor, 
            onset_mask_width, 
            typical_filtering,
            typical_mass,
            typical_min_tokens,
            beat_mask_width,
            beat_mask_downbeats,
            seed, 
            model_choice,
            n_mask_codebooks,
            pitch_shift_amt, 
            sample_cutoff, 
        }
  
    # connect widgets
    vamp_button.click(
        fn=vamp,
        inputs=_inputs,
        outputs=[output_audio, audio_mask],
    )

    api_vamp_button = gr.Button("api vamp", visible=False)
    api_vamp_button.click(
        fn=api_vamp, inputs=_inputs, outputs=[output_audio], api_name="vamp"
    )

    use_as_input_button.click(
        fn=lambda x: x, inputs=[output_audio], outputs=[input_audio]
    )


demo.launch(share=True, enable_queue=True, debug=True)
