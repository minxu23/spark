import Foundation

/// 给用户看的出错说明。手机 App、分享扩展和 Mac 应用共用（Mac 那边是指向这里的链接）
struct SparkError: LocalizedError {
    let message: String
    init(_ message: String) { self.message = message }
    var errorDescription: String? { message }
}
