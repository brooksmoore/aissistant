import Foundation

struct Message: Identifiable, Equatable {
    enum Role {
        case me
        case assistant
        case error
    }

    let id = UUID()
    let role: Role
    let text: String
    let date: Date = Date()
}
