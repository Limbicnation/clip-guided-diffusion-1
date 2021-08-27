from PIL.Image import Image as PILImage
from PIL import Image
import clip
import io
import requests
import argparse
import time
from functools import lru_cache
from pathlib import Path

from torch.nn.functional import normalize

from data.imagenet1000_clsidx_to_labels import IMAGENET_CLASSES

import torch as th
from torchvision.transforms import functional as tvf
from torchvision.transforms.transforms import ToTensor
from tqdm.auto import tqdm, trange

from cgd.clip_util import (CLIP_NORMALIZE, MakeCutouts, imagenet_top_n, load_clip)
from cgd.util import (CACHE_PATH, create_gif,
                      download_guided_diffusion, fetch, load_guided_diffusion,
                      log_image, spherical_dist_loss, tv_loss)

import sys
import os
sys.path.append(os.path.join(os.getcwd(), "guided-diffusion"))

TIMESTEP_RESPACINGS = ("25", "50", "100", "250", "500", "1000",
                       "ddim25", "ddim50", "ddim100", "ddim250", "ddim500", "ddim1000")
DIFFUSION_SCHEDULES = (25, 50, 100, 250, 500, 1000)
IMAGE_SIZES = (64, 128, 256, 512)
CLIP_MODEL_NAMES = ("ViT-B/16", "ViT-B/32", "RN50",
                    "RN101", "RN50x4", "RN50x16")

import torch as th
import torchvision as tv
from torch.nn import functional as tnf

def check_parameters(
    prompts: list,
    image_prompts: list,
    image_size: int,
    timestep_respacing: str,
    diffusion_steps: int,
    clip_model_name: str,
    save_frequency: int,
    noise_schedule: str,
):
    if not (len(prompts) > 0 or len(image_prompts) > 0):
        raise ValueError("Must provide at least one prompt, text or image.")
    if not (diffusion_steps in DIFFUSION_SCHEDULES):
        print('(warning) Diffusion steps should be one of:', DIFFUSION_SCHEDULES)
    if not (noise_schedule in ['linear', 'cosine']):
        raise ValueError('Noise schedule should be one of: linear, cosine')
    if not (clip_model_name in CLIP_MODEL_NAMES):
        raise ValueError(f"--clip model name should be one of: {CLIP_MODEL_NAMES}")
    if not (image_size in IMAGE_SIZES):
        raise ValueError(f"--image size should be one of {IMAGE_SIZES}")
    if not (0 < save_frequency <= int(timestep_respacing.replace('ddim', ''))):
        raise ValueError("--save_frequency must be greater than 0 and less than `timestep_respacing`")
    # TODO check that timestep_respacing is valid
    # if len(init_image) > 0 and skip_timesteps != 0:
    #     raise ValueError("skip_timesteps/-skip must be greater than 0 when using init_image")
    if not (timestep_respacing in TIMESTEP_RESPACINGS):
        print(f"Pausing run. `timestep_respacing` should be one of {TIMESTEP_RESPACINGS}. CTRL-C if this was a mistake.")
        time.sleep(5)
        print("Resuming run.")

# Define necessary functions

def fetch(url_or_path):
    if str(url_or_path).startswith('http://') or str(url_or_path).startswith('https://'):
        r = requests.get(url_or_path)
        r.raise_for_status()
        fd = io.BytesIO()
        fd.write(r.content)
        fd.seek(0)
        return fd
    return open(url_or_path, 'rb')


def parse_prompt(prompt): # parse a single prompt in the form "<text||img_url>:<weight>"
    if prompt.startswith('http://') or prompt.startswith('https://'):
        vals = prompt.rsplit(':', 2) # theres two colons, so we grab the 2nd
        vals = [vals[0] + ':' + vals[1], *vals[2:]]
    else:
        vals = prompt.rsplit(':', 1) # grab weight after colon
    vals = vals + ['', '1'][len(vals):] # if no weight, use 1
    return vals[0], float(vals[1]) # return text, weight

def encode_text_prompt(txt, weight, clip_model_name="ViT-B/32", device="cpu"):
    clip_model, _ = load_clip(clip_model_name, device)
    txt_tokens = clip.tokenize(txt).to(device)
    txt_encoded = clip_model.encode_text(txt_tokens).float()
    return txt_encoded, weight
    
def encode_image_prompt(image:str, weight: float, diffusion_size:int, num_cutouts, clip_model_name:str="ViT-B/32", device:str="cpu"):
    clip_model, clip_size = load_clip(clip_model_name, device)
    make_cutouts = MakeCutouts(cut_size=clip_size, num_cutouts=num_cutouts)
    pil_img = Image.open(fetch(image)).convert('RGB')
    smallest_side = min(diffusion_size, *pil_img.size)
    # You can ignore the type warning caused by pytorch resize having 
    # an incorrect type hint for their resize signature. which does indeed support PIL.Image
    pil_img = tvf.resize(pil_img, [smallest_side], tvf.InterpolationMode.LANCZOS)
    batch = make_cutouts(tvf.to_tensor(pil_img).unsqueeze(0).to(device))
    batch_embed = clip_model.encode_image(normalize(batch)).float()
    batch_weight = [weight / make_cutouts.cutn] * make_cutouts.cutn
    return batch_embed, batch_weight

def range_loss(input):
    return (input - input.clamp(-1, 1)).pow(2).mean([1, 2, 3])

def clip_guided_diffusion(
    prompts: "list[str]" = [],
    image_prompts: "list[str]" = [],
    batch_size: int = 1,
    tv_scale: float = 150,
    range_scale: float = 50,
    image_size: int = 128,
    class_cond: bool = True,
    clip_guidance_scale: int = 1000,
    cutout_power: float = 1.0,
    num_cutouts: int = 32,
    timestep_respacing: str = "1000",
    seed: int = 0,
    diffusion_steps: int = 1000,
    skip_timesteps: int = 0,
    init_image: str = "",
    # init_weight: float = 1.0,
    checkpoints_dir: str = CACHE_PATH,
    clip_model_name: str = "ViT-B/32",
    augs: list = [],
    randomize_class: bool = True,
    num_classes: int = 0, # If 0, use all classes
    prefix_path: str = 'outputs',
    save_frequency: int = 1,
    noise_schedule: str = "linear",
    dropout: float = 0.0,
    device: str = '',
):
    print()
    if len(device) == 0:
        device = 'cuda' if th.cuda.is_available() else 'cpu'
        print(f"Using device {device}. You can specify a device manually with `--device/-dev`")
    else:
        print(f"Using device {device}")
    fp32_diffusion = (device == 'cpu')

    if seed:
        th.manual_seed(seed)

    Path(prefix_path).mkdir(parents=True, exist_ok=True)
    Path(checkpoints_dir).mkdir(parents=True, exist_ok=True)

    diffusion_path = download_guided_diffusion(image_size=image_size, checkpoints_dir=checkpoints_dir, class_cond=class_cond)

    # Load CLIP model/Encode text/Create `MakeCutouts`
    embeds_list = []
    weights_list = []
    clip_model, clip_size = load_clip(clip_model_name, device)

    for prompt in prompts:
        text, weight = parse_prompt(prompt)
        text, weight = encode_text_prompt(text, weight, clip_model_name, device)
        embeds_list.append(text)
        weights_list.append(weight)

    for image_prompt in image_prompts:
        img, weight = parse_prompt(image_prompt)
        image_prompt, batched_weight = encode_image_prompt(img, weight, image_size, num_cutouts=num_cutouts, clip_model_name=clip_model_name, device=device)
        embeds_list.append(image_prompt)
        weights_list.extend(batched_weight)

    target_embeds = th.cat(embeds_list)

    weights = th.tensor(weights_list, device=device)
    if weights.sum().abs() < 1e-3: # smart :)
        raise RuntimeError('The weights must not sum to 0.')
    weights /= weights.sum().abs()

    make_cutouts = MakeCutouts(cut_size=clip_size, num_cutouts=num_cutouts, cutout_size_power=cutout_power, augment_list=augs)

    # Load initial image (if provided)
    init_tensor = None
    if len(init_image) > 0:
        # vgg_perceptual_loss = VGGPerceptualLoss(resize=True).to(device)
        pil_image = Image.open(fetch(init_image)).convert(
            "RGB").resize((image_size, image_size), Image.LANCZOS)
        init_tensor = ToTensor()(pil_image).to(device).unsqueeze(0).mul(2).sub(1)

   # Class randomization requires a starting class index `y`
    model_kwargs = {}
    if class_cond:
        model_kwargs["y"] = th.zeros([batch_size], device=device, dtype=th.long)

    # Load guided diffusion
    gd_model, diffusion = load_guided_diffusion(
        checkpoint_path=diffusion_path,
        image_size=image_size, class_cond=class_cond,
        diffusion_steps=diffusion_steps,
        timestep_respacing=timestep_respacing,
        use_fp16=(not fp32_diffusion),
        device=device,
        noise_schedule=noise_schedule,
        dropout=dropout,
    )
    custom_classes = [] # guided-diffusion will use all imagenet classes if this is empty
    if num_classes > 0:
        print(f"Using {num_classes} custom classes discovered by CLIP.")
        # TODO use more than just the first prompt
        custom_classes = imagenet_top_n(target_embeds, device, num_classes, clip_model_name=clip_model_name)

    current_timestep = None
    def cond_fn(x, t, y=None):
        print(f"Class '{IMAGENET_CLASSES[y[0].to(int)]}'" if class_cond else '')
        with th.enable_grad():
            x = x.detach().requires_grad_()
            n = x.shape[0]
            my_t = th.ones([n], device=device, dtype=th.long) * \
                current_timestep
            out = diffusion.p_mean_variance(
                gd_model, x, my_t, clip_denoised=False, model_kwargs={"y": y})
            fac = diffusion.sqrt_one_minus_alphas_cumprod[current_timestep]
            # Blend denoised prediction with noisey sample
            x_in = out["pred_xstart"] * fac + x * (1 - fac)
            clip_in = CLIP_NORMALIZE(make_cutouts(x_in.add(1).div(2)))
            cutout_embeds = clip_model.encode_image(clip_in).float().view([num_cutouts, n, -1])
            dists = spherical_dist_loss(cutout_embeds.unsqueeze(0), target_embeds.unsqueeze(0))
            dists = dists.view([num_cutouts, n, -1])
            losses = dists.mul(weights).sum(2).mean(0)
            # vgg_loss = vgg_perceptual_loss(x_in, init_tensor)
            tv_losses = tv_loss(x_in)
            range_losses = range_loss(out['pred_xstart'])
            loss = losses.sum() * clip_guidance_scale + tv_losses.sum() * tv_scale + range_losses.sum() * range_scale
            final_loss = -th.autograd.grad(loss, x)[0]
            return final_loss

    # Choose between normal or DDIM
    if timestep_respacing.startswith("ddim"):
        diffusion_sample_loop = diffusion.ddim_sample_loop_progressive
    else:
        diffusion_sample_loop = diffusion.p_sample_loop_progressive

    try:
        cgd_samples = diffusion_sample_loop(
            gd_model,
            (batch_size, 3, image_size, image_size),
            clip_denoised=False,
            model_kwargs=model_kwargs,
            cond_fn=cond_fn,
            progress=False,
            skip_timesteps=skip_timesteps,
            init_image=init_tensor,
            randomize_class=randomize_class,
            custom_classes=custom_classes,
        )
        # Gather generator for diffusion
        current_timestep = diffusion.num_timesteps - 1
        for step, sample in enumerate(cgd_samples):
            current_timestep -= 1
            if step % save_frequency == 0 or current_timestep == -1:
                for batch_idx, image_tensor in enumerate(sample["pred_xstart"]):
                    yield batch_idx, log_image(image_tensor, prefix_path, prompts, step, batch_idx)
        for batch_idx in range(batch_size):
            create_gif(prefix_path, prompts, batch_idx)

    except (RuntimeError, KeyboardInterrupt) as runtime_ex:
        if "CUDA out of memory" in str(runtime_ex):
            print(f"CUDA OOM error occurred.")
            print(
                f"Try lowering --image_size/-size, --batch_size/-bs, --num_cutouts/-cutn")
            print(
                f"--clip_model/-clip (currently {clip_model_name}) can have a large impact on VRAM usage.")
            print(f"'RN50' will use the least VRAM. 'ViT-B/32' the second least and is good for its memory/runtime constraints.")
        else:
            raise runtime_ex


def main():
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.ArgumentDefaultsHelpFormatter
    )
    p.add_argument("--prompts", "-txts", type=str, default='', 
                   help="the prompt/s to reward paired with weights. e.g. 'My text:0.5|Other text:-0.5' ")
    p.add_argument("--image_prompts", "-imgs", type=str, default='', 
                   help="the image prompt/s to reward paired with weights. e.g. 'img1.png:0.5,img2.png:-0.5'")
    p.add_argument("--image_size", "-size", type=int, default=128,
                   help="Diffusion image size. Must be one of [64, 128, 256, 512].")
    p.add_argument("--init_image", "-blend", type=str, default='',
                   help="Blend an image with diffusion for n steps")
    # p.add_argument("--init_weight", "-init_wt", type=float, default=1.0,
    #                help="Blend an image with diffusion for n steps")
    p.add_argument("--skip_timesteps", "-skip", type=int, default=0,
                   help="Number of timesteps to blend image for. CLIP guidance occurs after this.")
    p.add_argument("--prefix", "-dir", default="outputs",
                   type=Path, help="output directory")
    p.add_argument("--checkpoints_dir", "-ckpts", default=CACHE_PATH,
                   type=Path, help="Path subdirectory containing checkpoints.")
    p.add_argument("--batch_size", "-bs", type=int,
                   default=1, help="the batch size")
    p.add_argument("--clip_guidance_scale", "-cgs", type=float, default=1000,
                   help="Scale for CLIP spherical distance loss. Values will need tinkering for different settings.",)
    p.add_argument("--tv_scale", "-tvs", type=float,
                   default=100., help="Scale for denoising loss",)
    p.add_argument("--range_scale", "-rs", type=float,
                   default=50., help="Scale for denoising loss",)
    p.add_argument("--seed", "-seed", type=int,
                   default=0, help="Random number seed")
    p.add_argument("--save_frequency", "-freq", type=int,
                   default=1, help="Save frequency")
    p.add_argument("--diffusion_steps", "-steps", type=int,
                   default=1000, help="Diffusion steps")
    p.add_argument("--timestep_respacing", "-respace", type=str,
                   default="1000", help="Timestep respacing")
    p.add_argument("--num_cutouts", "-cutn", type=int, default=48,
                   help="Number of randomly cut patches to distort from diffusion.")
    p.add_argument("--cutout_power", "-cutpow", type=float,
                   default=0.5, help="Cutout size power")
    p.add_argument("--clip_model", "-clip", type=str, default="ViT-B/32",
                   help=f"clip model name. Should be one of: {CLIP_MODEL_NAMES}")
    p.add_argument("--uncond", "-uncond", action="store_true",
                   help='Use finetuned unconditional checkpoints from OpenAI (256px) and Katherine Crowson (512px)')
    p.add_argument("--noise_schedule", "-sched", default='linear', type=str,
                   help="Specify noise schedule. Either 'linear' or 'cosine'.")
    p.add_argument("--dropout", "-drop", default=0.0, type=float,
                   help="Amount of dropout to apply. ")
    p.add_argument("--max_classes", "-top", default=0, type=int)
    p.add_argument("--device", "-dev", default='', type=str, help="Device to use. Either cpu or cuda.")
    args = p.parse_args()

    _class_cond = not args.uncond
    prefix_path = args.prefix

    Path(prefix_path).mkdir(exist_ok=True)

    prompts = []
    if len(args.prompts) > 0:
        prompts = args.prompts.split('|')

    image_prompts = []
    if len(args.image_prompts) > 0:
        image_prompts = args.image_prompts.split(',')

    print(f"Given text prompts: {prompts}")
    print(f"Given image prompts: {image_prompts}")
    print(f'Given initial image: {args.init_image}')
    print("Using:")
    print("===")
    print(f"CLIP guidance scale: {args.clip_guidance_scale} ")
    print(f"TV Scale: {args.tv_scale}")
    print(f"Range scale: {args.range_scale}")
    print(f"Dropout: {args.dropout}.")
    print(f"Number of cutouts: {args.num_cutouts} number of cutouts.")
    cgd_generator = clip_guided_diffusion(
        prompts=prompts,
        image_prompts=image_prompts,
        batch_size=args.batch_size,
        tv_scale=args.tv_scale,
        range_scale=args.range_scale,
        image_size=args.image_size,
        class_cond=_class_cond,
        clip_guidance_scale=args.clip_guidance_scale,
        cutout_power=args.cutout_power,
        num_cutouts=args.num_cutouts,
        timestep_respacing=args.timestep_respacing,
        seed=args.seed,
        diffusion_steps=args.diffusion_steps,
        skip_timesteps=args.skip_timesteps,
        init_image=args.init_image,
        checkpoints_dir=args.checkpoints_dir,
        clip_model_name=args.clip_model,
        randomize_class=(_class_cond),
        noise_schedule=args.noise_schedule,
        dropout=args.dropout,
        device=args.device,
        augs=[],
        num_classes=args.max_classes,
    )
    prefix_path.mkdir(exist_ok=True)
    list(enumerate(tqdm(cgd_generator))) # iterate over generator
    for batch_idx in range(args.batch_size):
        create_gif(base=prefix_path,prompts=prompts, batch_idx=batch_idx)

if __name__ == "__main__":
    main()