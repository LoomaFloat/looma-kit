"""Looma для авторов приложений: SDK вызовов моделей и CLI `looma`.

    from loomafloat import Looma

    looma = Looma()
    print(looma.chat("Qwen3.8-Flash-Next-NVFP4", "Привет!"))

В процессе приложения ключ и адрес API платформа кладёт в окружение сама
(`LOOMA_APP_TOKEN`, `LOOMA_API_URL`); на машине автора — `looma login`.
"""

__version__ = "0.2.0"

from loomafloat.client import Apps, Looma, Video, client_of  # noqa: E402
from loomafloat.http import LoomaError  # noqa: E402

__all__ = ["Apps", "Looma", "LoomaError", "Video", "__version__", "client_of"]
