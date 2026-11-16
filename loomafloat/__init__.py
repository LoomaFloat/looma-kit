"""Looma Float для авторов сервисов: SDK вызовов моделей и CLI `looma`.

    from loomafloat import Looma

    looma = Looma()
    print(looma.chat("Qwen3.8-Flash-Next-NVFP4", "Привет!"))

В процессе сервиса ключ и адрес API платформа кладёт в окружение сама
(`LOOMA_APP_TOKEN`, `LOOMA_API_URL`); на машине автора — `looma login`.
"""

__version__ = "0.5.0"

from loomafloat.client import Answer, Apps, Job, Looma, Video, client_of  # noqa: E402
from loomafloat.http import LoomaError  # noqa: E402

__all__ = ["Answer", "Apps", "Job", "Looma", "LoomaError", "Video", "__version__",
           "client_of"]
