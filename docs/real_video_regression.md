# 真实视频回归测试清单

本清单只记录已授权 URL、任务 ID、结果路径和日志，不提交任何受版权保护的视频文件。

## 样本

| 样本 | URL / 本地路径 | 授权说明 | 任务 ID | 结果 |
| --- | --- | --- | --- | --- |
| B站 1 |  |  |  |  |
| B站 2 |  |  |  |  |
| YouTube |  |  |  |  |
| 5–15 秒本地视频 |  |  |  |  |
| 1 分钟内网络视频 |  |  |  |  |

## 验收命令

```bash
make doctor
make download-one URL='...' COOKIES_FROM_BROWSER=chrome
make status JOB_ID='...'
make extract JOB_ID='...'
make transcribe JOB_ID='...' ASR_BACKEND=mlx_whisper ASR_MODEL=small.en SOURCE_LANGUAGE=en
make translate JOB_ID='...' TRANSLATOR_PROVIDER=codex_cli
make synthesize JOB_ID='...' TTS_PROVIDER=edge
make align JOB_ID='...' MAX_TEMPO_FACTOR=1.8
make mux JOB_ID='...' KEEP_ORIGINAL_AUDIO=true DUCK_ORIGINAL_AUDIO=true
```

## 记录项

- 标题、时长、源格式、分辨率、音轨数；
- 下载耗时、失败重试次数、失败日志；
- ASR 模型、识别片段数、模糊片段数；
- 翻译后端、批次数、重试次数；
- TTS 后端、失败重试次数、自动缩写片段数；
- 最终输出路径、质量报告路径、人工听感备注。
