# vox2aff

把 Sound Voltex 的 VOX 谱面和 beatmania IIDX 的 `.1` 谱面转成 Arcaea AFF，并输出 Arcade Plus / Arcade Chan 能打开的工程文件夹。

需要本机已有的游戏数据。本工具不下载曲库。

## 环境

- Python 3.10 或更高
- [ffmpeg](https://ffmpeg.org/) 在 `PATH` 中（转封面和音频时需要）

```powershell
pip install -r requirements.txt
$env:PYTHONPATH = "src"
```

依赖：PySide6（界面）、kbinxml（读取旧版 IIDX `.ifs`）、numpy（混音）。

## 界面

```powershell
python src/vox2aff/gui.py
python src/iidx2aff/gui.py
```

选择游戏的 data 文件夹和输出文件夹，搜索歌曲后点「转换铺面」。


## 映射关系

**SDVX（4K）**：BT 1–4 是四条地面轨。FX Chip 是天空 note，FX 长按是半高的绿色 arc。

**SDVX（6K）**：BT 1–4 是四条地面 1、3、4、6 轨。FX 是 2、5轨。

**IIDX**：白键 1、3、5、7 是地面轨，黑键 2、4、6 是轨道交界上的天空 note。1P 的盘子在 1 轨左侧，2P 的盘子在 4 轨右侧，BSS 是绿色 arc。


## 输出

每首歌一个文件夹：

```text
曲目/
  0.aff … 4.aff
  base.ogg
  base.jpg
  Arcade/Project.arcade
```

