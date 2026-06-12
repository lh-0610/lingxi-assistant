"""MP4 → 透明 GIF（桌宠用）。

把纯色（默认纯白）背景的 MP4 抠图成透明 GIF，方便丢进 assets/desktop_pet/。
GIF 只支持二值 alpha（要么完全透明要么完全不透明），所以用阈值法：
RGB 三通道都 >= bg_threshold 的像素 → alpha=0；其余完全不透明。

用法（单次）：
    python scripts/mp4_to_pet_gif.py <input.mp4> <output.gif>
        [--height 320] [--fps 15] [--threshold 235] [--bg white|black]

或者直接 import + 调 convert()。
"""
import argparse
import os

import cv2
import numpy as np
from PIL import Image


def convert(
    mp4_path: str,
    gif_path: str,
    target_height: int = 320,
    target_fps: int = 15,
    bg_threshold: int = 235,
    bg: str = "white",
    alpha_cutoff: int = None,
    erode: int = None,
) -> dict:
    """
    bg_threshold: white 模式下 RGB 三通道都 >= 这个值视为背景；black 模式下都 <= (255-阈值) 视为背景。
    边界值要"宽"一点否则 JPEG/H.264 压缩噪声会让边缘留一圈灰边；太宽会把角色身上同色高光也抠掉。
    桌宠默认 320 高、15fps、235 阈值，对即梦/runway 生成的白底视频实测够用。

    alpha_cutoff: 二值化阈值（0-255）。LANCZOS 缩放后 alpha 半透明像素 ≥ cutoff 保留，否则透明。
        默认：white/black=128，green=160（更激进，吃掉绿溢出 fringe）。
    erode: 形态学腐蚀像素数。在二值化之后再把角色轮廓内缩 N 像素，去掉残留毛边。
        默认：white/black=0，green=1。
    """
    if alpha_cutoff is None:
        alpha_cutoff = 160 if bg == "green" else 128
    if erode is None:
        erode = 1 if bg == "green" else 0
    cap = cv2.VideoCapture(mp4_path)
    if not cap.isOpened():
        raise RuntimeError(f"无法打开视频: {mp4_path}")

    src_fps = cap.get(cv2.CAP_PROP_FPS) or 24.0
    src_total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    src_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    src_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))

    # 抽帧间隔：以原 fps 为基准，每 step 帧取一帧 ≈ target_fps
    step = max(1, round(src_fps / target_fps))
    out_fps = src_fps / step
    duration_ms = int(round(1000.0 / out_fps))

    # 等比缩放到目标高度
    scale = target_height / src_h
    out_w = max(1, int(round(src_w * scale)))
    out_h = target_height

    frames = []
    idx = 0
    while True:
        ok, frame_bgr = cap.read()
        if not ok:
            break
        if idx % step == 0:
            rgba = _bgr_to_keyed_rgba(frame_bgr, bg, bg_threshold)
            pil = Image.fromarray(rgba, mode="RGBA").resize(
                (out_w, out_h), Image.LANCZOS
            )
            # LANCZOS 会让 alpha 边缘出现半透明像素，但 GIF 只能 0/255，
            # 用阈值再 binarize 一次，避免边缘灰圈
            r, g, b, a = pil.split()
            a = a.point(lambda v, c=alpha_cutoff: 255 if v >= c else 0)
            # 腐蚀：把角色轮廓再缩 N 像素，吃掉绿溢出/锯齿残留
            if erode > 0:
                a_arr = np.array(a, dtype=np.uint8)
                kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
                for _ in range(erode):
                    a_arr = cv2.erode(a_arr, kernel)
                a = Image.fromarray(a_arr, mode="L")
            pil = Image.merge("RGBA", (r, g, b, a))
            frames.append(pil)
        idx += 1

    cap.release()
    if not frames:
        raise RuntimeError("没读到任何帧")

    # 写多帧 GIF。disposal=2 = 每帧前清空 canvas，避免残影把上帧抠掉的"窟窿"留下
    # transparency=0 = palette 第 0 个 slot 是透明色
    frames[0].save(
        gif_path,
        save_all=True,
        append_images=frames[1:],
        duration=duration_ms,
        loop=0,
        disposal=2,
        transparency=0,
        optimize=False,
    )

    size_kb = os.path.getsize(gif_path) / 1024
    return {
        "frames": len(frames),
        "size": (out_w, out_h),
        "fps": round(out_fps, 1),
        "kb": round(size_kb, 1),
    }


def _bgr_to_keyed_rgba(frame_bgr, bg: str, threshold: int) -> np.ndarray:
    """OpenCV BGR → RGBA ndarray，按背景颜色做 chroma-key。"""
    # BGR -> RGB
    rgb = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)
    h, w = rgb.shape[:2]
    if bg == "white":
        # 三通道都 >= 阈值 → 背景
        mask_bg = np.all(rgb >= threshold, axis=2)
    elif bg == "black":
        mask_bg = np.all(rgb <= (255 - threshold), axis=2)
    elif bg == "green":
        # 绿幕：G 是主导通道。用"相对绿"判定而不是绝对阈值，
        # 这样亮绿幕 (#00b140 G=177) 和暗森林绿 (G≈130) 都能识别。
        # 渐变 alpha：按"绿度分数"算 0~255 alpha，而不是硬阈值。
        # 这样边缘有平滑过渡，LANCZOS 缩放后再二值化能切出更干净的边。
        r = rgb[..., 0].astype(np.int16)
        g = rgb[..., 1].astype(np.int16)
        b = rgb[..., 2].astype(np.int16)
        # 绿度 = G 比 max(R, B) 多多少。负数 = 不绿，正数越大 = 越绿
        greenness = g - np.maximum(r, b)
        # < 10：肯定是角色（保留，alpha=255）
        # 10 ~ 50：过渡带（线性渐变 alpha 255→0）
        # > 50：肯定是背景（alpha=0）
        alpha_f = np.clip(1.0 - (greenness - 10) / 40.0, 0.0, 1.0)
        # G 太暗的地方（< 50）不当背景（避免误抠黑色头饰之类）
        alpha_f = np.where(g < 50, 1.0, alpha_f)
        mask_bg = greenness >= 50  # 用于 despill 步骤判断哪些是"明确背景"
    else:
        raise ValueError(f"未知背景类型: {bg}")
    if bg == "green":
        alpha = (alpha_f * 255).astype(np.uint8)
    else:
        alpha = np.where(mask_bg, 0, 255).astype(np.uint8)

    # 去绿溢出 (despill)：绿幕反光会让角色边缘像素带轻微绿色。
    # 对所有"保留"像素，如果 G > max(R, B)，把 G 压到 max(R, B)。
    # 这样：1) 真彩色像素（红丝带/皮肤等）G 本来就不是最高，不受影响
    #       2) 绿溢出像素 G 异常高，被压下来，绿毛边消失
    if bg == "green":
        keep = ~mask_bg
        rr = rgb[..., 0].astype(np.int16)
        gg = rgb[..., 1].astype(np.int16)
        bb = rgb[..., 2].astype(np.int16)
        max_rb = np.maximum(rr, bb)
        spill = keep & (gg > max_rb)
        new_g = np.where(spill, max_rb, gg).astype(np.uint8)
        rgb = np.dstack([rgb[..., 0], new_g, rgb[..., 2]])

    rgba = np.dstack([rgb, alpha])
    return rgba


def main():
    p = argparse.ArgumentParser(description="纯色背景 MP4 → 透明 GIF（桌宠用）")
    p.add_argument("input", help="输入 .mp4 路径")
    p.add_argument("output", help="输出 .gif 路径")
    p.add_argument("--height", type=int, default=320, help="目标高度像素 (默认 320)")
    p.add_argument("--fps", type=int, default=15, help="目标帧率 (默认 15)")
    p.add_argument("--threshold", type=int, default=235,
                   help="背景判定阈值 0-255 (white 模式下 RGB 三通道都 >= 这个值 → 透明)")
    p.add_argument("--bg", choices=["white", "black", "green"], default="white",
                   help="背景颜色 (默认 white)")
    p.add_argument("--alpha-cutoff", type=int, default=None,
                   help="alpha 二值化阈值 0-255（默认 white/black=128, green=160；调高吃掉更多边缘 fringe）")
    p.add_argument("--erode", type=int, default=None,
                   help="角色轮廓腐蚀像素数（默认 white/black=0, green=1；调高彻底去掉绿毛边但会削角色）")
    args = p.parse_args()

    info = convert(args.input, args.output,
                   target_height=args.height, target_fps=args.fps,
                   bg_threshold=args.threshold, bg=args.bg,
                   alpha_cutoff=args.alpha_cutoff, erode=args.erode)
    print(f"完成: {args.output}")
    print(f"  {info['frames']} 帧 / {info['size'][0]}x{info['size'][1]} / {info['fps']}fps / {info['kb']} KB")


if __name__ == "__main__":
    main()
