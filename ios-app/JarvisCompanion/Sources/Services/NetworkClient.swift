import Foundation

enum NetworkError: LocalizedError {
    case notConfigured
    case badResponse
    case server(String)

    var errorDescription: String? {
        switch self {
        case .notConfigured:
            return "Set the server address and secret in Settings first."
        case .badResponse:
            return "The server sent back something unexpected."
        case .server(let message):
            return message
        }
    }
}

/// Talks to webhook.py's /capture and /health endpoints — the exact same
/// path a Siri Shortcut already uses, so a message from this app gets the
/// identical brain.respond() handling (tool calls, empty-promise guard,
/// budget caps) as anything typed into Telegram. No parallel capture logic.
struct NetworkClient {
    var baseURL: String
    var secret: String

    private func request(path: String, method: String) throws -> URLRequest {
        guard !baseURL.isEmpty, !secret.isEmpty,
              let url = URL(string: baseURL.trimmingCharacters(in: .init(charactersIn: "/")) + path)
        else { throw NetworkError.notConfigured }
        var req = URLRequest(url: url, timeoutInterval: 30)
        req.httpMethod = method
        req.setValue(secret, forHTTPHeaderField: "X-Aissistant-Secret")
        return req
    }

    func checkHealth() async -> Bool {
        guard let req = try? request(path: "/health", method: "GET") else { return false }
        guard let (data, response) = try? await URLSession.shared.data(for: req),
              let http = response as? HTTPURLResponse, http.statusCode == 200,
              let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any]
        else { return false }
        return (json["ok"] as? Bool) == true
    }

    func send(text: String) async throws -> String {
        var req = try request(path: "/capture", method: "POST")
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.httpBody = try JSONSerialization.data(withJSONObject: ["text": text])

        let (data, response) = try await URLSession.shared.data(for: req)
        guard let http = response as? HTTPURLResponse else { throw NetworkError.badResponse }
        guard let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else {
            throw NetworkError.badResponse
        }
        let ok = (json["ok"] as? Bool) == true
        if http.statusCode == 200, ok, let reply = json["reply"] as? String {
            return reply
        }
        let serverError = (json["error"] as? String) ?? (json["reply"] as? String) ?? "Server error (\(http.statusCode))."
        throw NetworkError.server(serverError)
    }
}
