# 桌宠资源说明

本目录存放桌面悬浮宠物的立绘和动画。**仓库自带一套默认桌宠动画**（作者用 AI 生成），开箱即用；想换成自己的，按下面同名文件覆盖即可。

## 需要的文件

应用按以下优先级查找资源：

```
1. assets/desktop_pet/idle_desktop_pet_final.gif      ← idle（待机）GIF
   assets/desktop_pet/thinking_desktop_pet_final.gif  ← thinking（思考）GIF
   assets/desktop_pet/wave_desktop_pet_final.gif      ← wave（挥手）GIF

2. 全部缺失时降级到：
   assets/desktop_pet/lingxi_pet.png  ← 单张立绘（无动画）
```

## 自己制作

最简单的：用灵犀内置的 `generate_image` 工具让 AI 给你画一张立绘，PNG 透明背景。

进阶（生成 GIF 动画）：
- 用即梦 / Runway / Kling 生成 5 秒视频，纯白/黑/绿背景
- 跑 `python scripts/mp4_to_pet_gif.py 视频.mp4 assets/desktop_pet/idle_desktop_pet_final.gif` 一键转透明 GIF

## 推荐规格

| 类型 | 尺寸 | 帧数 | 时长 | 格式 |
|------|------|------|------|------|
| GIF | 高 320px（脚本默认） | 60-120 | 5 秒一循环 | 透明背景（1-bit alpha） |
| 静态 PNG | ~1024×1536 | - | - | RGBA |

## 调速

如果觉得动画太快/太慢，改 `config.json` 的 `pet_animation_speed`：
- 默认 `1.0` = GIF 原速
- `0.5` = 慢 2 倍（呼吸节奏）
- `2.0` = 快 2 倍
