import SwiftUI

struct ContentView: View {
    @StateObject private var vm = ChatViewModel()
    @State private var showSettings = false
    @Environment(\.scenePhase) private var scenePhase

    var body: some View {
        NavigationView {
            VStack(spacing: 0) {
                if !vm.isConfigured {
                    VStack(spacing: 12) {
                        Text("Not set up yet")
                            .font(.headline)
                        Text("Add your server address and secret in Settings to connect.")
                            .font(.subheadline)
                            .foregroundColor(.secondary)
                            .multilineTextAlignment(.center)
                        Button("Open Settings") { showSettings = true }
                            .buttonStyle(.borderedProminent)
                    }
                    .padding()
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
                } else {
                    ScrollViewReader { proxy in
                        ScrollView {
                            LazyVStack(alignment: .leading, spacing: 10) {
                                ForEach(vm.messages) { message in
                                    MessageBubble(message: message)
                                        .id(message.id)
                                }
                            }
                            .padding()
                        }
                        .onChange(of: vm.messages) { messages in
                            if let last = messages.last {
                                withAnimation { proxy.scrollTo(last.id, anchor: .bottom) }
                            }
                        }
                    }

                    if vm.isSending {
                        HStack(spacing: 6) {
                            ProgressView()
                            Text("Jarvis is thinking…")
                                .font(.caption)
                                .foregroundColor(.secondary)
                        }
                        .padding(.horizontal)
                        .padding(.top, 4)
                    }

                    Divider()

                    HStack(spacing: 8) {
                        TextField("Message Jarvis…", text: $vm.draft)
                            .textFieldStyle(.roundedBorder)
                        Button {
                            Task { await vm.send() }
                        } label: {
                            Image(systemName: "arrow.up.circle.fill")
                                .font(.system(size: 28))
                        }
                        .disabled(vm.draft.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty || vm.isSending)
                    }
                    .padding()
                }
            }
            .navigationTitle("Jarvis")
            .toolbar {
                ToolbarItem(placement: .navigationBarLeading) {
                    connectionDot
                }
                ToolbarItem(placement: .navigationBarTrailing) {
                    Button {
                        showSettings = true
                    } label: {
                        Image(systemName: "gearshape")
                    }
                }
            }
            .sheet(isPresented: $showSettings) {
                SettingsView(onSaved: { await vm.refreshConnection() })
            }
            .task {
                await vm.refreshConnection()
            }
            .onChange(of: scenePhase) { phase in
                if phase == .active {
                    Task { await vm.refreshConnection() }
                }
            }
        }
        .navigationViewStyle(.stack)
    }

    @ViewBuilder
    private var connectionDot: some View {
        switch vm.isConnected {
        case .some(true):
            Label("Connected", systemImage: "circle.fill")
                .labelStyle(.iconOnly)
                .foregroundColor(.green)
                .font(.caption)
        case .some(false):
            Label("Not reachable", systemImage: "circle.fill")
                .labelStyle(.iconOnly)
                .foregroundColor(.red)
                .font(.caption)
        case .none:
            EmptyView()
        }
    }
}

private struct MessageBubble: View {
    let message: Message

    var body: some View {
        HStack {
            if message.role == .me { Spacer(minLength: 40) }
            Text(message.text)
                .padding(.horizontal, 12)
                .padding(.vertical, 8)
                .background(background)
                .foregroundColor(foreground)
                .cornerRadius(14)
            if message.role != .me { Spacer(minLength: 40) }
        }
    }

    private var background: Color {
        switch message.role {
        case .me: return .accentColor
        case .assistant: return Color(.secondarySystemBackground)
        case .error: return Color.red.opacity(0.15)
        }
    }

    private var foreground: Color {
        switch message.role {
        case .me: return .white
        case .assistant: return .primary
        case .error: return .red
        }
    }
}

struct ContentView_Previews: PreviewProvider {
    static var previews: some View {
        ContentView()
    }
}
