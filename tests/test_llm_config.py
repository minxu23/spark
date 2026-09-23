"""两个 app 共用的后端参数解析：key 的查找顺序、必填项校验、默认值。"""

import unittest
from unittest import mock

from core import keys, llm_config
from core.llm import DEFAULT_OLLAMA_HOST, OPENROUTER_API_BASE


class ResolveTests(unittest.TestCase):
    def setUp(self):
        p1 = mock.patch.object(keys, "read_key_file", return_value="")
        p2 = mock.patch.dict("os.environ", {"ANTHROPIC_API_KEY": "", "OPENROUTER_API_KEY": ""})
        for p in (p1, p2):
            p.start()
            self.addCleanup(p.stop)

    def test_默认后端由调用方决定(self):
        self.assertEqual(llm_config.resolve({}, default_backend="cli")["backend"], "cli")
        with self.assertRaises(llm_config.ConfigError):
            llm_config.resolve({}, default_backend="api")

    def test_anthropic_key_依次找请求_环境变量_key文件(self):
        cfg = llm_config.resolve({"backend": "api", "api_key": " typed "}, default_backend="api")
        self.assertEqual(cfg["api_key"], "typed")
        with mock.patch.dict("os.environ", {"ANTHROPIC_API_KEY": "sk-env"}):
            self.assertEqual(llm_config.resolve({"backend": "api"}, default_backend="api")["api_key"], "sk-env")
        with mock.patch.object(keys, "read_key_file", return_value="sk-file"):
            self.assertEqual(llm_config.resolve({"backend": "api"}, default_backend="api")["api_key"], "sk-file")

    def test_不需要调模型时不校验必填项(self):
        cfg = llm_config.resolve({"backend": "api"}, default_backend="api", needs_llm=False)
        self.assertEqual(cfg["api_key"], "")

    def test_openrouter_读环境变量_补默认地址_缺模型报错(self):
        with mock.patch.dict("os.environ", {"OPENROUTER_API_KEY": "sk-or"}):
            cfg = llm_config.resolve({"backend": "openrouter", "model": "m"}, default_backend="api")
            self.assertEqual((cfg["api_key"], cfg["api_base"]), ("sk-or", OPENROUTER_API_BASE))
            with self.assertRaises(llm_config.ConfigError):
                llm_config.resolve({"backend": "openrouter"}, default_backend="api")

    def test_openrouter_本地key不发给自定义地址_手填的key可以(self):
        with mock.patch.object(keys, "read_key_file", return_value="sk-or-file"):
            with self.assertRaises(llm_config.ConfigError):
                llm_config.resolve({"backend": "openrouter", "model": "m", "api_base": "https://evil/v1"},
                                   default_backend="api")
            cfg = llm_config.resolve({"backend": "openrouter", "model": "m", "api_key": "typed",
                                      "api_base": "https://proxy/v1"}, default_backend="api")
            self.assertEqual(cfg["api_base"], "https://proxy/v1")

    def test_第三方兼容api三项都要填(self):
        for missing in ("api_key", "api_base", "model"):
            data = {"backend": "openai_compatible", "api_key": "k", "api_base": "b", "model": "m"}
            data.pop(missing)
            with self.assertRaises(llm_config.ConfigError, msg=missing):
                llm_config.resolve(data, default_backend="api")

    def test_ollama_默认本机地址_要填模型(self):
        cfg = llm_config.resolve({"backend": "ollama", "model": "qwen"}, default_backend="api")
        self.assertEqual(cfg["api_base"], DEFAULT_OLLAMA_HOST)
        with self.assertRaises(llm_config.ConfigError):
            llm_config.resolve({"backend": "ollama"}, default_backend="api")


if __name__ == "__main__":
    unittest.main()
