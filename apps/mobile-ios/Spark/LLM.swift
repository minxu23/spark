import Foundation
import Security

/// 手机上直接调模型：复盘和出报告在手机上跑时用。跟电脑端 core/llm.py 同一套规矩：
/// Anthropic 关掉思考、输出到上限就放大再试一次；OpenAI 兼容接口（含 OpenRouter）空正文时放大上限再试；
/// 去掉漏出来的 <think>、中文和英文数字之间的空格；返回空文本当失败。
enum LLM {
    enum Provider: String, CaseIterable, Identifiable {
        case anthropic, openrouter, openaiCompatible = "openai_compatible"
        var id: String { rawValue }
        var label: String {
            switch self {
            case .anthropic: "Anthropic（Claude）"
            case .openrouter: "OpenRouter"
            case .openaiCompatible: "OpenAI 兼容接口"
            }
        }
        var defaultModel: String {
            switch self {
            case .anthropic: "claude-sonnet-5-5"
            case .openrouter: "anthropic/claude-sonnet-5.5"
            case .openaiCompatible: ""
            }
        }
        var suggestedModels: [String] {
            switch self {
            case .anthropic: ["claude-sonnet-5-5", "claude-opus-5-5", "claude-haiku-5-5", "claude-fable-5-1"]
            case .openrouter: ["anthropic/claude-sonnet-5.5", "anthropic/claude-opus-5.5", "anthropic/claude-haiku-5.5"]
            case .openaiCompatible: []
            }
        }
    }

    /// 选哪家、哪个模型、接口地址存在 UserDefaults；Key 只在钥匙串里
    struct Config: Equatable {
        var provider: Provider
        var model: String
        var apiBase: String
        var key: String

        static let openrouterBase = "https://openrouter.ai/api/v1"
        private static let service = "com.minxu.spark.llm"

        static func load() -> Config? {
            let d = UserDefaults.standard
            guard let p = Provider(rawValue: d.string(forKey: "llm.provider") ?? ""),
                  let key = readKey(p), !key.isEmpty else { return nil }
            return Config(provider: p, model: d.string(forKey: "llm.model.\(p.rawValue)") ?? p.defaultModel,
                          apiBase: d.string(forKey: "llm.base.\(p.rawValue)") ?? "", key: key)
        }

        /// 各家的设置分开存，切换时不用重填
        static func stored(_ p: Provider) -> (model: String, apiBase: String, hasKey: Bool) {
            let d = UserDefaults.standard
            return (d.string(forKey: "llm.model.\(p.rawValue)") ?? p.defaultModel,
                    d.string(forKey: "llm.base.\(p.rawValue)") ?? "", readKey(p) != nil)
        }

        /// key 为空表示沿用钥匙串里已有的
        static func save(provider p: Provider, model: String, apiBase: String, key: String) throws {
            let d = UserDefaults.standard
            d.set(p.rawValue, forKey: "llm.provider")
            d.set(model.trimmingCharacters(in: .whitespacesAndNewlines), forKey: "llm.model.\(p.rawValue)")
            d.set(apiBase.trimmingCharacters(in: .whitespacesAndNewlines), forKey: "llm.base.\(p.rawValue)")
            let k = key.trimmingCharacters(in: .whitespacesAndNewlines)
            guard !k.isEmpty else { return }
            if k.unicodeScalars.contains(where: { CharacterSet.whitespacesAndNewlines.contains($0) || $0.value < 32 }) {
                throw SparkError("Key 里夹着空格或换行，请检查一下")
            }
            deleteKey(p)
            let q: [String: Any] = [kSecClass as String: kSecClassGenericPassword, kSecAttrService as String: service,
                                    kSecAttrAccount as String: p.rawValue,
                                    kSecAttrAccessible as String: kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly,
                                    kSecValueData as String: Data(k.utf8)]
            let status = SecItemAdd(q as CFDictionary, nil)
            if status != errSecSuccess { throw SparkError("Key 存不进钥匙串（\(status)）") }
        }

        static func deleteKey(_ p: Provider) {
            SecItemDelete([kSecClass as String: kSecClassGenericPassword, kSecAttrService as String: service,
                           kSecAttrAccount as String: p.rawValue] as CFDictionary)
        }

        private static func readKey(_ p: Provider) -> String? {
            let q: [String: Any] = [kSecClass as String: kSecClassGenericPassword, kSecAttrService as String: service,
                                    kSecAttrAccount as String: p.rawValue,
                                    kSecReturnData as String: true, kSecMatchLimit as String: kSecMatchLimitOne]
            var out: CFTypeRef?
            guard SecItemCopyMatching(q as CFDictionary, &out) == errSecSuccess, let data = out as? Data else { return nil }
            return String(decoding: data, as: UTF8.self)
        }

        /// 报告、复盘里写的「模型后端」
        var label: String {
            let name = ["anthropic": "Anthropic API", "openrouter": "OpenRouter", "openai_compatible": "第三方 API"][provider.rawValue] ?? ""
            return model.isEmpty ? name : "\(name)（\(model)）"
        }
    }

    struct Failure: LocalizedError {
        let message: String
        var errorDescription: String? { message }
    }

    /// 思考型模型把上限耗在思考上、正文为空时，放大到这么多再试一次
    private static let thinkingRetryMaxTokens = 32000
    /// Anthropic 输出到上限时放大到这么多再试一次（不再多，免得一次重试太贵）
    private static let anthropicRetryMaxTokens = 20000
    /// 各模型「关掉思考」认哪种写法：disabled / between_tools / 不带。几篇同时摘取时会并发读写，加锁
    nonisolated(unsafe) private static var thinkingOff: [String: String?] = [:]
    private static let lock = NSLock()

    /// images：JPEG 图片，纯图片的文章写摘要时给模型看（模型不支持看图时接口会报错）
    static func complete(_ prompt: String, images: [Data] = [], config c: Config, maxTokens: Int = 4000, timeout: TimeInterval = 900) async throws -> String {
        try Task.checkCancellation()
        var text: String
        switch c.provider {
        case .anthropic:
            text = try await anthropic(prompt, images, c, maxTokens: maxTokens, timeout: timeout)
        case .openrouter:
            text = try await openAICompatible(prompt, images, c, base: c.apiBase.isEmpty ? Config.openrouterBase : c.apiBase,
                                              maxTokens: maxTokens, timeout: timeout, openrouter: true)
        case .openaiCompatible:
            guard !c.apiBase.isEmpty else { throw Failure(message: "OpenAI 兼容接口要填接口地址") }
            guard !c.model.isEmpty else { throw Failure(message: "OpenAI 兼容接口要填模型名") }
            text = try await openAICompatible(prompt, images, c, base: c.apiBase, maxTokens: maxTokens, timeout: timeout, openrouter: false)
        }
        text = stripThink(text)
        if text.isEmpty {
            throw Failure(message: "模型返回了空内容（可能是这次要生成的内容较长、超过了这次调用的输出上限），请重试")
        }
        return stripCJKLatinSpacing(text)
    }

    // MARK: 各家接口

    private static func post(_ url: URL, headers: [String: String], body: [String: Any], timeout: TimeInterval) async throws -> (Int, Data) {
        var req = URLRequest(url: url, timeoutInterval: timeout)
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        for (k, v) in headers { req.setValue(v, forHTTPHeaderField: k) }
        req.httpBody = try JSONSerialization.data(withJSONObject: body)
        do {
            let (data, resp) = try await URLSession.shared.data(for: req)
            return ((resp as? HTTPURLResponse)?.statusCode ?? 0, data)
        } catch let e as URLError where e.code == .cancelled {
            throw CancellationError()
        } catch let e as URLError {
            throw Failure(message: "连不上模型接口：\(e.localizedDescription)")
        }
    }

    private static func detail(_ data: Data) -> String {
        String(decoding: data.prefix(800), as: UTF8.self)
    }

    private static func anthropic(_ prompt: String, _ images: [Data], _ c: Config, maxTokens: Int, timeout: TimeInterval) async throws -> String {
        let content: Any = images.isEmpty ? prompt : images.map {
            ["type": "image", "source": ["type": "base64", "media_type": "image/jpeg", "data": $0.base64EncodedString()]] as [String: Any]
        } + [["type": "text", "text": prompt]]
        let model = c.model.isEmpty ? Provider.anthropic.defaultModel : c.model
        let url = URL(string: "https://api.anthropic.com/v1/messages")!
        let headers = ["x-api-key": c.key, "anthropic-version": "2023-06-01"]

        func create(_ tokens: Int) async throws -> [String: Any] {
            var tried: String? = lock.withLock { thinkingOff[model] } ?? "disabled"
            while true {
                var body: [String: Any] = ["model": model, "max_tokens": tokens,
                                           "messages": [["role": "user", "content": content]]]
                if let tried { body["thinking"] = ["type": tried] }
                let (status, data) = try await post(url, headers: headers, body: body, timeout: timeout)
                if status == 200, let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
                    lock.withLock { thinkingOff[model] = .some(tried) }
                    return obj
                }
                let msg = detail(data)
                // 只在报错确实指向 thinking 参数时换写法重试；额度、鉴权、模型名写错照实报
                guard status == 400, msg.lowercased().contains("thinking") else {
                    throw Failure(message: "Anthropic API 调用失败（HTTP \(status)）：\(msg)")
                }
                if tried == "disabled", msg.contains("between_tools") { tried = "between_tools" }
                else if tried != nil { tried = nil }
                else { throw Failure(message: "Anthropic API 调用失败：\(msg)") }
            }
        }

        func text(_ obj: [String: Any]) -> String {
            ((obj["content"] as? [[String: Any]]) ?? []).filter { $0["type"] as? String == "text" }
                .compactMap { $0["text"] as? String }.joined().trimmingCharacters(in: .whitespacesAndNewlines)
        }

        var resp = try await create(maxTokens)
        if resp["stop_reason"] as? String == "max_tokens", maxTokens < anthropicRetryMaxTokens {
            resp = try await create(anthropicRetryMaxTokens)
        }
        if resp["stop_reason"] as? String == "max_tokens" {
            throw Failure(message: "模型输出达到长度上限，正文尚未完成；请缩小单次生成范围后重试（未保存截断正文）")
        }
        return text(resp)
    }

    private struct HTTPStatus: Error { let code: Int; let message: String }

    private static func openAICompatible(_ prompt: String, _ images: [Data], _ c: Config, base: String, maxTokens: Int,
                                         timeout: TimeInterval, openrouter: Bool) async throws -> String {
        guard !c.model.isEmpty else { throw Failure(message: "没有填模型名") }
        let trimmed = base.trimmingCharacters(in: CharacterSet(charactersIn: "/ "))
        guard let url = URL(string: trimmed.hasSuffix("/chat/completions") ? trimmed : trimmed + "/chat/completions"),
              url.scheme == "https" || url.scheme == "http" else { throw Failure(message: "接口地址不对：\(base)") }
        let headers = ["Authorization": "Bearer \(c.key)", "Accept": "application/json"]
        let content: Any = images.isEmpty ? prompt : [["type": "text", "text": prompt]] + images.map {
            ["type": "image_url", "image_url": ["url": "data:image/jpeg;base64,\($0.base64EncodedString())"]] as [String: Any]
        }

        func send(_ tokens: Int, _ reasoning: [String: Any]?) async throws -> (String, String) {
            var body: [String: Any] = ["model": c.model, "messages": [["role": "user", "content": content]], "max_tokens": tokens]
            if let reasoning { body["reasoning"] = reasoning }
            let (status, data) = try await post(url, headers: headers, body: body, timeout: timeout)
            guard status == 200 else { throw HTTPStatus(code: status, message: "模型接口请求失败（HTTP \(status)）：\(detail(data))") }
            guard let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
                  let choice = (obj["choices"] as? [[String: Any]])?.first,
                  let message = choice["message"] as? [String: Any] else {
                throw Failure(message: "模型接口返回格式不兼容：\(detail(data))")
            }
            var content = ""
            if let s = message["content"] as? String { content = s }
            else if let parts = message["content"] as? [[String: Any]] { content = parts.compactMap { $0["text"] as? String }.joined() }
            return (content.trimmingCharacters(in: .whitespacesAndNewlines), choice["finish_reason"] as? String ?? "")
        }

        var reasoning: [String: Any]? = openrouter ? ["effort": "none"] : nil
        var result: (String, String)
        do {
            result = try await send(maxTokens, reasoning)
        } catch let e as HTTPStatus {
            // 强制思考的模型不接受关掉思考：不带这个参数再发一次
            guard reasoning != nil, [400, 422].contains(e.code) else { throw Failure(message: e.message) }
            reasoning = nil
            do { result = try await send(maxTokens, nil) } catch let e2 as HTTPStatus { throw Failure(message: e2.message) }
        }
        if result.0.isEmpty, result.1 == "length", maxTokens < thinkingRetryMaxTokens {
            let retry = reasoning ?? (openrouter ? ["effort": "low"] : nil)
            if let r = try? await send(thinkingRetryMaxTokens, retry) { result = r }
        }
        if result.0.isEmpty {
            let extra = result.1 == "length" ? "（思考过程占满了输出上限，换个非思考型模型或减少单次输入）" : "（finish_reason=\(result.1.isEmpty ? "未知" : result.1)）"
            throw Failure(message: "模型接口返回空内容\(extra)")
        }
        return result.0
    }

    // MARK: 清理输出

    /// 去掉混进正文的思考过程（同 core/llm.py 的 strip_think）
    static func stripThink(_ s: String) -> String {
        var t = s.replacingOccurrences(of: #"(?is)<think>.*?</think>"#, with: "", options: .regularExpression)
        if let close = t.range(of: "</think>", options: [.caseInsensitive, .backwards]) { t = String(t[close.upperBound...]) }
        if let open = t.range(of: "<think>", options: .caseInsensitive) { t = String(t[..<open.lowerBound]) }
        return t.trimmingCharacters(in: .whitespacesAndNewlines)
    }

    /// 中文和紧挨着的英文、数字之间的空格去掉；代码块里的不动
    static func stripCJKLatinSpacing(_ s: String) -> String {
        let cjk = "\\x{4E00}-\\x{9FFF}\\x{3400}-\\x{4DBF}"
        let space = "(?<=[\(cjk)])[ \\t]+(?=[A-Za-z0-9])|(?<=[A-Za-z0-9])[ \\t]+(?=[\(cjk)])"
        guard let code = try? NSRegularExpression(pattern: "```[\\s\\S]*?```|`[^`\\n]*`"),
              let sp = try? NSRegularExpression(pattern: space) else { return s }
        let ns = s as NSString
        var out = "", last = 0
        func clean(_ r: NSRange) -> String {
            let part = ns.substring(with: r)
            return sp.stringByReplacingMatches(in: part, range: NSRange(location: 0, length: (part as NSString).length), withTemplate: "")
        }
        for m in code.matches(in: s, range: NSRange(location: 0, length: ns.length)) {
            out += clean(NSRange(location: last, length: m.range.location - last)) + ns.substring(with: m.range)
            last = m.range.location + m.range.length
        }
        out += clean(NSRange(location: last, length: ns.length - last))
        return out
    }
}
