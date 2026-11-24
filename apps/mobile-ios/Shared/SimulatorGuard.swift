#if DEBUG && targetEnvironment(simulator)
import SwiftUI

/// 模拟器 DEBUG 上没说用样例库还是真实数据（Fixture.isBlocked）时的说明页。主 App（SimulatorGuardApp）和分享扩展共用。
/// 只用系统字体和固定的字，不读任何存储。真机、Release 构建里没有这个文件的内容
struct SimulatorGuardView: View {
    /// 分享扩展里给「知道了」按钮，点了结束；主 App 里是 nil
    var onClose: (() -> Void)? = nil

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            Text(Fixture.guardTitle)
                .font(.title2.weight(.semibold))
            Text(Fixture.guardHowTo)
            Text(Fixture.guardReal)
                .foregroundStyle(.secondary)
            if let onClose {
                Button("知道了", action: onClose)
                    .buttonStyle(.borderedProminent)
                    .padding(.top, 8)
            }
        }
        .font(.body)
        .padding(24)
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .leading)
        .background(Color(.systemBackground))
        .accessibilityIdentifier("simulator-guard")
    }
}
#endif
