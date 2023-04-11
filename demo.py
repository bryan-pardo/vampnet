from pathlib import Path
from typing import Tuple
import yaml

import numpy as np
import audiotools as at
import argbind

import gradio as gr
from vampnet.interface import Interface

conf = yaml.safe_load(Path("conf/interface-jazzpop-exp.yml").read_text())

Interface = argbind.bind(Interface)
AudioLoader = argbind.bind(at.data.datasets.AudioLoader)
with argbind.scope(conf):
    interface = Interface()
    loader = AudioLoader()

dataset = at.data.datasets.AudioDataset(
    loader,
    sample_rate=interface.codec.sample_rate,
    duration=interface.coarse.chunk_size_s,
    n_examples=5000,
    without_replacement=True,
)


def load_audio(file):
    print(file)
    filepath = file.name
    sig = at.AudioSignal.salient_excerpt(
        filepath, 
        duration=interface.coarse.chunk_size_s
    )
    sig = interface.preprocess(sig)

    audio = sig.samples.numpy()[0]
    sr = sig.sample_rate
    return sr, audio.T

def load_random_audio():
    index = np.random.randint(0, len(dataset))
    sig = dataset[index]["signal"]
    sig = interface.preprocess(sig)

    audio = sig.samples.numpy()[0]
    sr = sig.sample_rate
    return sr, audio.T


def vamp(
    input_audio, prefix_s, suffix_s, rand_mask_intensity,
    mask_periodic_amt, beat_unmask_dur,
    mask_dwn_chk, dwn_factor,
    mask_up_chk, up_factor, 
    num_vamps, mode
):
    try:
        print(input_audio)

        sig = at.AudioSignal(
            input_audio[1], 
            sample_rate=input_audio[0]
        )
        
        if beat_unmask_dur > 0.0:
            beat_mask = interface.make_beat_mask(
                sig, 
                before_beat_s=0.01,
                after_beat_s=beat_unmask_dur,
                mask_downbeats=mask_dwn_chk,
                mask_upbeats=mask_up_chk,
                downbeat_downsample_factor=dwn_factor, 
                beat_downsample_factor=up_factor,
                dropout=0.7, 
                invert=True
            )
        else:
            beat_mask = None

        if mode == "standard": 
            zv = interface.coarse_vamp_v2(
                sig, 
                prefix_dur_s=prefix_s,
                suffix_dur_s=suffix_s,
                num_vamps=num_vamps,
                downsample_factor=mask_periodic_amt,
                intensity=rand_mask_intensity,
                ext_mask=beat_mask
            )
        elif mode == "loop":
            zv = interface.loop(
                zv, 
                prefix_dur_s=prefix_s, 
                suffix_dur_s=suffix_s, 
                num_loops=num_vamps,
                downsample_factor=mask_periodic_amt,
                intensity=rand_mask_intensity,
                ext_mask=beat_mask
            )

        zv = interface.coarse_to_fine(zv)
        sig = interface.to_signal(zv)
        return sig.sample_rate, sig.samples[0].T
    except Exception as e:
        raise gr.Error(f"failed with error: {e}")
        


with gr.Blocks() as demo:

    gr.Markdown('# Vampnet')
    
    with gr.Row():
        # input audio
        with gr.Column():
            gr.Markdown("## Input Audio")

            manual_audio_upload = gr.File(
                label=f"upload some audio (will be randomly trimmed to max of {interface.coarse.chunk_size_s:.2f}s)",
                file_types=["audio"]
            )
            load_random_audio_button = gr.Button("or load random audio")

            input_audio = gr.Audio(
                label="input audio",
                interactive=False, 
            )
            input_audio_viz = gr.HTML(
                label="input audio",
            )

            # connect widgets
            load_random_audio_button.click(
                fn=load_random_audio,
                inputs=[],
                outputs=[ input_audio]
            )

            manual_audio_upload.change(
                fn=load_audio,
                inputs=[manual_audio_upload],
                outputs=[ input_audio]
            )
                

        # mask settings
        with gr.Column():
            gr.Markdown("## Mask Settings")
            prefix_s = gr.Slider(
                label="prefix length (seconds)",
                minimum=0.0,
                maximum=10.0,
                value=0.0
            )
            suffix_s = gr.Slider(
                label="suffix length (seconds)",
                minimum=0.0,
                maximum=10.0,
                value=0.0
            )

            rand_mask_intensity = gr.Slider(
                label="random mask intensity (lower means more freedom)",
                minimum=0.0,
                maximum=1.0,
                value=1.0
            )

            mask_periodic_amt = gr.Slider(
                label="periodic unmasking factor (higher means more freedom)",
                minimum=0,
                maximum=32, 
                step=1,
                value=2, 
            )
            compute_mask_button = gr.Button("compute mask")
            mask_output = gr.Audio(
                label="masked audio",
                interactive=False,
                visible=False
            )
            mask_output_viz = gr.Video(
                label="masked audio",
                interactive=False
            )
        
        with gr.Column():
            gr.Markdown("## Beat Unmasking")
            with gr.Accordion(label="beat unmask"):
                beat_unmask_dur = gr.Slider(
                    label="duration", 
                    minimum=0.0,
                    maximum=3.0,
                    value=0.1
                )
                with gr.Accordion("downbeat settings"):
                    mask_dwn_chk = gr.Checkbox(
                        label="unmask downbeats",
                        value=True
                    )
                    dwn_factor = gr.Slider(
                        label="downbeat downsample factor (unmask every Nth downbeat)",
                        value=1, 
                        minimum=1,
                        maximum=16, 
                        step=1
                    )
                with gr.Accordion("upbeat settings"):
                    mask_up_chk = gr.Checkbox(
                        label="unmask upbeats",
                        value=True
                    )
                    up_factor = gr.Slider(
                        label="upbeat downsample factor (unmask every Nth upbeat)",
                        value=1,
                        minimum=1,
                        maximum=16,
                        step=1
                    )
            
    # process and output
    with gr.Row():
        with gr.Column():
            gr.Markdown("**NOTE**: for loop mode, both prefix and suffix must be greater than 0.")
            mode = gr.Radio(
                label="mode",
                choices=["standard", "loop"],
                value="standard"
            )
            num_vamps = gr.Number(
                label="number of vamps",
                value=1,
                precision=0
            )
            vamp_button = gr.Button("vamp")

            output_audio = gr.Audio(
                label="output audio",
                interactive=False,
                visible=False
            )

    # connect widgets
    vamp_button.click(
        fn=vamp,
        inputs=[input_audio,
            prefix_s, suffix_s, rand_mask_intensity, 
            mask_periodic_amt, beat_unmask_dur, 
            mask_dwn_chk, dwn_factor, 
            mask_up_chk, up_factor, 
            num_vamps, mode
        ],
        outputs=[output_audio]
    )


demo.launch(share=True, server_name="0.0.0.0")