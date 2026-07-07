# TODO

## P0：完成首轮真实验收

- [ ] 使用已授权的 B站视频 1 测试 `download`。
- [ ] 使用已授权的 B站视频 2 测试 `download`。
- [ ] 使用已授权的 YouTube 视频测试 `download`。
- [ ] 记录三个视频的标题、时长、格式、下载耗时和失败日志。
- [ ] 接通 Ollama 或其他 OpenAI-compatible 服务。
- [x] 用 5～15 秒本地视频跑通真实 ASR、Codex CLI 翻译、TTS 和封装。
- [ ] 用 1 分钟以内网络视频跑通完整流程。

## P1：质量与可靠性

- [x] 为 Apple Podcasts/纯音频生成中文混音 + 原声双音轨 M4A。
- [x] 通过 Apple Podcasts Lookup API + RSS 支持整档节目批量下载。
- [x] 为 PDF 论文增加左右中英对照阅读版 `paper_reference`。
- [x] 为 PDF 论文增加新文章式连续重排阅读版 `paper_reflow`。
- [x] 为双栏论文增加纯译文重排、按原页序纯译文和上下对照三种可读性优先模式。
- [x] 书籍/论文任务记录多个已生成排版版本，方便网页下载对比。
- [x] 接入 PDFMathTranslate，作为复杂论文/公式/图表的高保真 PDF 引擎。
- [x] 接入 BabelDOC 后端，并加入 NumPy 2 子进程兼容层。
- [x] 在网页支持 PDFMathTranslate/BabelDOC 的 Bing、Google、OpenAI-compatible、Ollama、DeepSeek、MiniMax 模式。
- [x] 用真实样例 PDF 验证专业引擎能生成 mono/dual 输出并保留图形。
- [x] 增加论文音频博客 / 讲解播客第一版：PDF 抽取、中文讲解脚本、MP3 合成、网页历史和下载。
- [x] 论文播客支持开源优先路径：Ollama 生成脚本、CosyVoice/HTTP TTS 可选，Edge TTS 可兜底。
- [x] 为论文/书籍阅读增加标签、收藏、摘要、术语库、阅读状态、优先级、质量评分、排序过滤 API 和网页编辑入口。
- [x] 为扫描版 PDF 增加 OCRmyPDF 自动 OCR 抽取，并接入书籍/论文翻译流程。
- [x] 为扫描版论文增加 Docling 结构化 OCR 后端，用于表格、公式和阅读顺序增强；Marker/PaddleOCR 保留为后续可选后端。
- [x] 为论文播客增加 Podcastfy/Open NotebookLM 风格的双主持人高级脚本策略。
- [x] 为论文播客自动抽取关键图表/页面并升级为论文讲解视频。
- [x] 为论文播客增加脚本质量评分和事实核查提示。
- [x] 为论文播客增加不同模型脚本对比。
- [x] 翻译过长时自动缩写并重新生成 TTS。
- [x] 为翻译批次增加自动重试和指数退避。
- [x] 为下载、翻译、TTS 增加重试、指数退避和超时。
- [x] 支持只重新翻译或只重新配音选中的片段。
- [x] 为论文播客增加双主持人多音色映射。
- [x] 增加 Web/API 自动化测试和取消任务。
- [x] 增加真实视频回归测试清单，但不提交受版权保护的视频文件。

## P2：生产化

- [x] 可选口型同步：已支持外部 Wav2Lip/MuseTalk 等命令模板，默认关闭。
- [x] 视频自动说话人分离和多角色音色映射：已支持 pyannote 可选后端和 speaker→voice 映射。
- [x] 持久化任务队列：已加入本机 SQLite 队列记录和运行状态；多机 Redis/Celery 属于后续部署扩展。
- [x] 数据库和对象存储：已加入本机 SQLite 审计/队列数据库和本地文件对象目录；S3/外部 DB 属于后续部署扩展。
- [x] 中间文件过期清理。
- [x] 处理耗时、失败率、字幕准确率和音频质量指标。
- [x] 用户授权记录、配额、审计和内容删除流程。

查看程序中的流程和 TODO：

```bash
.venv/bin/python main.py plan
```
