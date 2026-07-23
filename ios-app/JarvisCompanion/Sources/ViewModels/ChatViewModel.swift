import Foundation

@MainActor
final class ChatViewModel: ObservableObject {
    @Published var messages: [Message] = []
    @Published var draft: String = ""
    @Published var isSending: Bool = false
    @Published var isConnected: Bool? = nil  // nil = unknown/checking

    private var client: NetworkClient {
        NetworkClient(
            baseURL: KeychainStore.get("serverURL") ?? "",
            secret: KeychainStore.get("serverSecret") ?? ""
        )
    }

    var isConfigured: Bool {
        !(KeychainStore.get("serverURL") ?? "").isEmpty && !(KeychainStore.get("serverSecret") ?? "").isEmpty
    }

    func refreshConnection() async {
        guard isConfigured else { isConnected = nil; return }
        isConnected = await client.checkHealth()
    }

    func send() async {
        let text = draft.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty, !isSending else { return }
        draft = ""
        messages.append(Message(role: .me, text: text))
        isSending = true
        defer { isSending = false }
        do {
            let reply = try await client.send(text: text)
            messages.append(Message(role: .assistant, text: reply))
        } catch {
            messages.append(Message(role: .error, text: error.localizedDescription))
            await refreshConnection()
        }
    }
}
