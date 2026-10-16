小黄花 · 谱室 / SheetSage2 local demo
====================================

独立的本机音频转谱能力，不启动 QQ/网易云客户端，不读登录态，
不启动或重启 Usic Electron，不改播放引擎、资料库和听歌统计。

打开
----
在 Usic 项目根目录运行：
  npm run score-lab

会打开浏览器；已有同一谱室服务时复用，不重复启动。
服务默认使用随机回环端口；实际地址见终端或 .private/score-lab/server.json。
首次启动的终端关闭/Ctrl+C 会关闭该服务；仅关闭浏览器标签页不关闭服务。
不会安装系统服务、开机启动或修改防火墙。

不依赖 npm/Electron 也可直接运行：
  python -m capabilities.score_lab.server --open

配置
----
复制 capabilities/score_lab/config.example.json 到 .private/score-lab/config.json。
USIC_SCORE_LAB_CONFIG 可指定其他配置文件。所有相对路径均相对 Usic 项目根，
不依赖当前工作目录、用户名或盘符。路径也可显式配置为绝对路径。

dataDir：谱室私有数据；默认 .private/score-lab，与播放器资料库分离。
python：仅用于后台模型子进程的 Python；HTTP 服务自身只依赖 Python 标准库。
sheetRepo / baseRepo：本机 SheetSage2 / MERT-v2-FullSong 完整模型目录。
assetsDir：本机 abcjs 和钢琴采样文件。
device：cpu 或 cuda；本机实际验收为 CPU/FP32，未验证本 demo 的 GPU 路径。
threads：CPU 线程数，默认 2；不抢满机器。
port：默认 0 自动选择空闲端口；固定端口被占用则拒绝启动，不抢占其他服务。
jobTimeoutSeconds：默认每个任务最长 3600 秒，可配置 60–7200 秒；
一个模型任务运行，最多 3 个排队。音频本身最长 15 分钟，与推理超时不是一回事。

新设备准备（显式下载；不要在播放器的 .venv 里安装这些依赖）
------------------------------------------------------------
示例为 Windows Python 3.12，模型约 2.76 GB，CPU 推理另需数 GB 内存：
  py -3.12 -m venv .private/runtime-sheetsage
  .private/runtime-sheetsage/Scripts/python.exe -m pip install torch==2.8.0 torchaudio==2.8.0 --index-url https://download.pytorch.org/whl/cpu
  .private/runtime-sheetsage/Scripts/python.exe -m pip install -r capabilities/score_lab/requirements.txt

在获得相应许可/访问权后，从作者官方模型仓库下载完整快照，不绕过门禁：
  m-a-p/SheetSage2 @ 32c7c7473e7b59e1ed36bd8a8fefbd11ae8d468a
  m-a-p/MERT-v2-FullSong @ d8ba1c745e733b3908ce6ad16ebeb17ac7600a42

用 huggingface-hub 的 snapshot_download(repo_id, revision, local_dir=...) 或其官方
CLI 保存到配置指定目录。目录名应为 Python 标识符（示例 sheetsage2、mert_base）。
此 demo 通过已配置的本机代码包直接加载模型，规避 transformers 4.45.2 的
嵌套本地代码缓存问题。它会运行模型仓库的 Python 代码，应先确认来源可信。
执行前按 model.lock.json 验证代码、配置、权重；不自动升级到未知版本。

仅下载谱面/钢琴资源（默认从固定官方 HF revision 下载）：
  python -m capabilities.score_lab.fetch_assets
网络不通时，可显式选择作者在 ModelScope 的公开镜像：
  python -m capabilities.score_lab.fetch_assets --mirror modelscope
镜像 manifest 和每个文件仍须与固定 SHA-256 相同，不能代替受限模型授权。
模型推理和页面播放本身不访问外网，不使用 CDN、云推理或云音频上传。

使用
----
左侧选择片段。原曲/钢琴回放切换保留当前秒数；钢琴回放来自模型 MIDI 音符，
不是把原曲换个标签播放。可单听人声旋律、器乐旋律或和弦；没有识别到的声部
禁用，不补造音符。和弦钢琴是模型和弦的规则配音，不等于完整伴奏扒谱。

五线谱增加独立的低音谱表「和弦伴奏」：展示上述和弦 MIDI 实际使用的音高，
保留重新起音和各音自己的延音线；不是只写 Em / Cmaj7 等名称。
沿用模型的拍点和小节，支持短小节、调号变化、跨小节延音。
这没有额外识别吉他扫弦、钢琴分解和弦、鼓点或每件乐器的真实演奏细节。
原曲、钢琴音频、原始 MIDI 和模型原始 ABC 均不因补谱而改变。

播放/暂停、拖动、前后 5 秒、全片段循环、0.5–1.25 倍速、音量可用。
空格播放/暂停（非输入框或按钮焦点），左右键前后 5 秒。
五线谱按小节高亮，点谱面音符跳到对应小节；不是逐音符精确谱面同步。
「音符」视图展示原始 MIDI 秒数和逐音符活动状态。
倍速使用浏览器原生 preservesPitch，实际音色仍取决于浏览器实现。

「导入音频」选择本机 MP3/WAV/FLAC/OGG/AIFF（64 MB 内），指定片段起点，
可选 30 秒、90 秒或整首。整首从 0 秒开始，最多 15 分钟；超过则明确拒绝，
不悄悄截成 30 秒。长曲由模型自身做重叠窗口拼接，保留原始歌曲时钟；
页面显示实际窗口进度，不伪造百分比。音符视图采用可横向滚动的有限宽画布，
避免整首曲的超大画布空白或超出浏览器尺寸限制。
本机本次 CPU/FP32 实测：
  《主角》王菲：300.288 秒整首，推理 308.31 秒，2 个窗口，94 小节；
    343 个人声旋律音符、100 个器乐旋律音符、500 个和弦配音音符。
  《喜欢寂寞》苏打绿：264.7075 秒整首，推理 199.94 秒，1 个窗口，77 小节；
    434 个人声旋律音符、93 个器乐旋律音符、434 个和弦配音音符。
两首均为真实运行，不是把 30 秒结果复制延长；没有人工真值，不代表准确率。
原有 30 秒片段约 1.5–2.5 分钟；以上是推理时间，不含启动/钢琴采样渲染，
不承诺其他歌曲/机器同样速度，也不是实时识别。
任务失败时私有 worker.log 留详细原因；页面不显示账号、绝对路径或内部日志。
所有输入/输出只放 dataDir，不覆盖输入原文件，也不回填播放器听歌记录。

导出：原始 MIDI（可分声部）、含伴奏的 ABC / MusicXML、模型原始 ABC、
旋律 MusicXML、原始时间音符 JSON。
含伴奏文件为 arrangement.abc / arrangement.musicxml；原始 score.abc 不覆盖。
MusicXML 按声部独立转换，再恢复短小节、调号和逐音符延音线，读回验证通过
才发布；避免多声部串成一条旋律，或短小节被自动补长后越播越偏。
后台模型未能生成 ABC/MusicXML 时仍可使用已有音符回放；不伪造成功乐谱。
重新渲染已完成的片段（不再推理）：
  <模型Python> -m capabilities.score_lab.worker --sample <片段ID> --rerender
只补齐已有结果的伴奏谱与导出（不再推理，不改任何音频/MIDI）：
  <模型Python> -m capabilities.score_lab.worker --sample <片段ID> --notation-only
CLI 本地导入：
  <模型Python> -m capabilities.score_lab.worker --sample <新ID> --audio <音频路径> --title <歌名> --artist <歌手> --offset 30
CLI 识别整首：
  <模型Python> -m capabilities.score_lab.worker --sample <新ID> --audio <音频路径> --title <歌名> --full
CLI --seconds 90 识别长片段；--full 配合 --offset 可识别从该起点到音频末尾。
CLI --reuse 只用于已有实测结果，必须匹配完全相同输入 WAV 的 SHA-256；
provenance.reused=true，保留原推理耗时，不声称重新推理。

时钟与准确性
------------
播放严格使用模型序列化 MIDI 的原始秒数；不把 ABC 固定 BPM 用作原曲时钟。
ABC 是模型量化的谱面，存在拍速半倍/倍速、八度、音长和漏音等可能误差。
原始结果不自动移调，也不把其他模型/人工假设覆盖为「纠正后的识别」。
本机样本没有人工真值，不提供识别准确率；音符多不等于更准。
钢琴使用 FluidR3 的采样音色，做共享响度/峰值保护和 35ms 收尾，
不是恢复原歌手音色，也未接入人声分离或自然语言纠错。

边界与许可
----------
只绑定 127.0.0.1。校验 Host、同源 Origin 和随机写入 token；不开放 CORS。
音频支持 Range，媒体/导出仅暴露完成样本的白名单文件；不提供任意文件读取。
关闭服务只终止它自己创建的模型子进程，不接触播放器或其他员工的服务。
Windows 模型子进程在加载前加入专属 Job，关闭或崩溃时连同其后代一起退出；
不按进程名批量结束 Python。私有数据目录带 OS 独占锁，避免两份服务互相覆盖。
崩溃后的排队/运行任务会显示已中断，不永久假装仍在识别。

SheetSage2 和 MERT-v2-FullSong 权重为 CC BY-NC 4.0，当前是本机个人研究 demo，
并非可直接随商业产品分发的授权。将来商用需单独确认模型许可或更换模型。
Usic 自写集成代码沿用仓库 GPL-3.0-or-later；模型、音乐、缓存和账号不入 Git。
abcjs 6.6.3：MIT，notices/abcjs-MIT.txt。
FluidR3 GM 钢琴采样：Frank Wen，CC BY 3.0 US，notices/piano-attribution.txt。
资源版本、来源、SHA：render-assets.lock.json。依赖保留安装包自带许可。

官方来源：
https://huggingface.co/m-a-p/SheetSage2
https://huggingface.co/m-a-p/MERT-v2-FullSong
https://github.com/paulrosen/abcjs
https://github.com/paulrosen/midi-js-soundfonts

验证
----
  python -m unittest tests.test_score_lab
  node --test tests/test_score_lab_timeline.mjs
  <模型Python> -m unittest capabilities.score_lab.test_renderer capabilities.score_lab.test_accompaniment
边界测试只用明确标注的合成夹具，不代表模型准确率。
实际歌曲的输入指纹、模型版本、推理耗时、音频能量、渲染来源保存在各片段
result.json/provenance。QA 截图和测试记录保留在忽略提交的 output/。
此次补谱另外核对了全部 5 个真实样本的原曲/钢琴/MIDI/原始 ABC 文件
是否保持字节不变（实际核对数量见 output/score-lab-chords-full-verification.json）；
两首完整音频的帧数、跨窗口覆盖、各声部和导出谱的音高/音长/延音亦独立读回。
