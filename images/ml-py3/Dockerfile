# ML 运行库镜像：Python 源码 Bot 的可选运行库变体（numpy/scipy/onnxruntime/torch CPU）。
#
# 分发模式：重型库在本镜像层，Bot 只上传源码；权重文件走用户云存储对局内
# 只读挂载（/mnt/data、/app/data）。沙箱资源档（1 CPU/512 MiB）不变——
# 选型必须满足 import RSS 门禁，见下方自检步骤。
#
# tag 追加式（与 botbattle-builder 同策略）：一经发布不得改动已发布 tag 的
# 内容；升级库版本 = 新 tag + limits.py 白名单追加 + 发布说明。
FROM botbattle-builder:bookworm-1

# 全部精确锁版；numpy 固定 1.x（torch 2.1 的兼容边界）。PyPI 默认 Linux
# torch wheel 是 CUDA 版（连带 ~2GB nvidia-* 依赖），CPU 版必须走 +cpu 索引。
# 部署机直连 PyPI/pytorch.org 严重限速（实测 ~25 KB/s），通用包走阿里云
# PyPI 镜像、torch 走上海交大 pytorch-wheels 镜像；不可达时可用 --build-arg 换源。
ARG PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/
ARG TORCH_INDEX_URL=https://mirror.sjtu.edu.cn/pytorch-wheels/cpu
# PIP_INDEX_URL 只作用于构建命令（环境变量前缀），不持久化进镜像。
RUN PIP_INDEX_URL=$PIP_INDEX_URL \
        python3 -m pip install --no-cache-dir --break-system-packages \
        numpy==1.26.4 scipy==1.11.4 onnxruntime==1.17.3 \
    && python3 -m pip install --no-cache-dir --break-system-packages \
        torch==2.1.2+cpu --index-url $TORCH_INDEX_URL

# 构建时选型自检：torch 栈（torch+numpy）import 后峰值 RSS 必须 ≤ 400 MiB，
# 为 512 MiB 沙箱档留出 Bot 自身与模型的余量；超标则构建失败、换更低版本。
RUN python3 -c "import resource, numpy, torch; rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0; print('torch stack import RSS = %.0f MiB' % rss); assert rss <= 400, 'ML 选型门禁失败: %.0f MiB' % rss; print('torch', torch.__version__)"
RUN python3 -c "import resource, numpy, scipy, onnxruntime, torch; rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0; print('full stack import RSS = %.0f MiB' % rss); assert rss <= 400, 'ML 选型门禁失败(全栈): %.0f MiB' % rss; print('onnxruntime', onnxruntime.__version__, 'torch', torch.__version__)"
