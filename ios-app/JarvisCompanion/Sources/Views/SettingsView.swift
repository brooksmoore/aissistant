import SwiftUI

struct SettingsView: View {
    @Environment(\.dismiss) private var dismiss
    @State private var serverURL: String = KeychainStore.get("serverURL") ?? ""
    @State private var secret: String = KeychainStore.get("serverSecret") ?? ""
    @State private var testResult: String?
    @State private var isTesting = false
    @State private var saveError: String?
    var onSaved: () async -> Void = {}

    var body: some View {
        NavigationView {
            Form {
                Section {
                    TextField("http://100.x.x.x:8766", text: $serverURL)
                        .textInputAutocapitalization(.never)
                        .disableAutocorrection(true)
                        .keyboardType(.URL)
                    SecureField("Shared secret (WEBHOOK_SECRET)", text: $secret)
                        .textInputAutocapitalization(.never)
                        .disableAutocorrection(true)
                } header: {
                    Text("Server")
                } footer: {
                    Text("Matches WEBHOOK_SECRET in instances/jarvis/.env on your Mac (jarvis's webhook runs on port 8766). This app only ever talks to this one address — stored in the Keychain, not in plain text.")
                }

                Section {
                    Button {
                        Task { await testConnection() }
                    } label: {
                        HStack {
                            Text("Test Connection")
                            if isTesting { Spacer(); ProgressView() }
                        }
                    }
                    .disabled(serverURL.isEmpty || secret.isEmpty || isTesting)
                    if let testResult = testResult {
                        Text(testResult)
                            .font(.caption)
                            .foregroundColor(testResult.hasPrefix("✅") ? .green : .red)
                    }
                    if let saveError = saveError {
                        Text(saveError)
                            .font(.caption)
                            .foregroundColor(.red)
                    }
                }
            }
            .navigationTitle("Settings")
            .toolbar {
                ToolbarItem(placement: .confirmationAction) {
                    Button("Save") {
                        let trimmedURL = serverURL.trimmingCharacters(in: .whitespacesAndNewlines)
                        let trimmedSecret = secret.trimmingCharacters(in: .whitespacesAndNewlines)
                        KeychainStore.set(trimmedURL, forKey: "serverURL")
                        KeychainStore.set(trimmedSecret, forKey: "serverSecret")
                        guard KeychainStore.get("serverURL") == trimmedURL,
                              KeychainStore.get("serverSecret") == trimmedSecret else {
                            saveError = "Couldn't save to Keychain — try again."
                            return
                        }
                        Task {
                            await onSaved()
                            dismiss()
                        }
                    }
                }
                ToolbarItem(placement: .cancellationAction) {
                    Button("Cancel") { dismiss() }
                }
            }
        }
    }

    private func testConnection() async {
        isTesting = true
        defer { isTesting = false }
        let ok = await NetworkClient(baseURL: serverURL, secret: secret).checkHealth()
        testResult = ok ? "✅ Connected" : "❌ Couldn't reach the server — check the address, secret, and that you're on the same tailnet/Wi-Fi."
    }
}
