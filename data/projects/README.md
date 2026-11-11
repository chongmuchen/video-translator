# 视频翻译项目目录

每个子目录对应一项翻译任务。目录名也是恢复时使用的名称。

| 项目 | 项目目录 | 成片位置 |
| --- | --- | --- |
| 猎天使魔女 · 神谷英树 69 段导演解说 | [bayonetta-69](bayonetta-69/) | [按集数播放](bayonetta-69/delivery/按集数播放.m3u8) |
| 樱井政博 A 系列 | [sakurai-a](sakurai-a/) | [播放顺序](sakurai-a/delivery/播放顺序.m3u8) |
| 樱井政博 B–H 系列 | [sakurai-b-h](sakurai-b-h/) | [按集数播放](sakurai-b-h/delivery/按集数播放.m3u8) |
| 樱井政博 I–N 系列 | [sakurai-i-n](sakurai-i-n/) | [按集数播放](sakurai-i-n/delivery/按集数播放.m3u8) |
| 我为什么先用文字设计关卡 | [hl2-text-first](hl2-text-first/) | [成片和双语字幕](hl2-text-first/delivery/) |
| 如何制作一个像王权陨落的一样简单又复杂的游戏 | [thronefall](thronefall/) | [成片和双语字幕](thronefall/delivery/) |

每个项目的 `delivery/` 存放最终 MP4 和双语字幕，系列项目还附有播放列表。`repair/` 存放逐句译稿、时间码、配音和修订脚本。单片的 `job/` 另存原始媒体、原文和任务记录。

以《猎天使魔女》为例，在 Finder 将 `data/projects/bayonetta-69` 整个文件夹拷到外置盘。拷完可从外置盘打开 `delivery/按集数播放.m3u8`。如果要逐文件比较，可在仓库根目录运行下面的命令，将第二个路径换成外置盘上的实际位置。命令只比较文件，无差异时无输出。

```sh
rsync -rnc --delete --exclude='.DS_Store' --exclude='._*' --itemize-changes \
  "data/projects/bayonetta-69/" "/Volumes/Elements/翻译视频/bayonetta-69/"
```

确认拷贝后，移走本机的 `data/projects/bayonetta-69` 即可回收空间。需要修订时，将整包放回 `data/projects/bayonetta-69`，再从仓库根目录运行该项目说明中的命令。旧的 `data/outputs/` 和 `work/` 入口是指向项目包的链接，放回同名目录即可重新使用。拷贝时选用 `data/projects/` 下的实际文件夹。

修订脚本共用本仓库的 Python 环境、FFmpeg 和 `work/render-support/`。配音缓存可复用已生成的语句。修改台词后，新增语音由该项目使用的 TTS 服务或本机语音合成。
