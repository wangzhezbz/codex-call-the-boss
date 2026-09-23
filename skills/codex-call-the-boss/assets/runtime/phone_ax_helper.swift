import ApplicationServices
import Foundation

// This helper only attaches to an already-running process by PID. It never
// asks LaunchServices to open an app, bundle, extension, or .xpc package.

// Bound read-only inspection, including individual AX IPC calls. An incomplete
// tree cannot prove that a call is absent or that a button is unique.
let inspectionDeadline = ProcessInfo.processInfo.systemUptime + 1.5
func inspectionMessageTimeout(remaining: Double) -> Float {
    return Float(min(0.75, max(0, remaining)))
}

func checkInspectionBudget() {
    let remaining = inspectionDeadline - ProcessInfo.processInfo.systemUptime
    if remaining <= 0 {
        fail("Phone.app accessibility inspection timed out; no action performed", code: 75)
    }
    // Only this helper's default changes. Let a slow valid IPC finish without
    // allowing any individual read to outlive the original total budget.
    let timeoutError = AXUIElementSetMessagingTimeout(AXUIElementCreateSystemWide(),
        inspectionMessageTimeout(remaining: remaining))
    guard timeoutError == .success else {
        fail("Could not bound accessibility messaging; no action performed", code: 75)
    }
}

// Attribute names are constants, not contact labels or UI contents.
var inspectedAttribute = "unknown"
func checkReadError(_ error: AXError) {
    if error != .success && error != .attributeUnsupported && error != .noValue {
        fail("Phone.app accessibility inspection unavailable (\(error.rawValue)); attribute=\(inspectedAttribute); no action performed", code: 75)
    }
}

struct AXNode {
    let element: AXUIElement
    let role: String
    let title: String
    let description: String
    let help: String
    let identifier: String
    let value: String
    let actions: [String]

    var searchableText: String {
        [role, title, description, help, identifier, value]
            .joined(separator: " ")
            .lowercased()
    }
}

func copyAttribute(_ element: AXUIElement, _ attribute: CFString) -> CFTypeRef? {
    checkInspectionBudget()
    inspectedAttribute = attribute as String
    var value: CFTypeRef?
    let error = AXUIElementCopyAttributeValue(element, attribute, &value)
    checkReadError(error)
    guard error == .success else {
        return nil
    }
    return value
}

func stringAttribute(_ element: AXUIElement, _ attribute: CFString) -> String {
    guard let value = copyAttribute(element, attribute) else { return "" }
    if let text = value as? String { return text }
    if CFGetTypeID(value) == CFNumberGetTypeID() {
        return String(describing: value)
    }
    return ""
}

func actionNames(_ element: AXUIElement) -> [String] {
    checkInspectionBudget()
    inspectedAttribute = "actionNames"
    var values: CFArray?
    let error = AXUIElementCopyActionNames(element, &values)
    checkReadError(error)
    guard error == .success,
          let names = values as? [String]
    else {
        return []
    }
    return names
}

func childElements(_ element: AXUIElement) -> [AXUIElement] {
    guard let value = copyAttribute(element, kAXChildrenAttribute as CFString),
          let children = value as? [AXUIElement]
    else {
        return []
    }
    return children
}

func shouldReadValueAttribute(role: String, actions: [String]) -> Bool {
    // A non-actionable group is a container, not a call control or text value.
    // Phone.app advertises AXValue on such a group but returns AXError.failure.
    // Still inspect its labels, actions and every child; all required reads fail closed.
    return role != (kAXGroupRole as String) || !actions.isEmpty
}

func collectNodes(_ root: AXUIElement, maxDepth: Int = 14, maxNodes: Int = 800,
                  buttonsOnly: Bool = false) -> [AXNode] {
    var result: [AXNode] = []
    var stack: [(AXUIElement, Int)] = [(root, 0)]
    var visited = 0
    while !stack.isEmpty {
        guard visited < maxNodes else {
            fail("Phone.app accessibility tree exceeded node limit; no action performed", code: 75)
        }
        let (element, depth) = stack.removeLast()
        visited += 1
        let role = stringAttribute(element, kAXRoleAttribute as CFString)
        guard !role.isEmpty else {
            fail("Phone.app accessibility role unavailable; no action performed", code: 75)
        }
        // Confirmation can only match AXButton. Traverse every role/child,
        // but do not query unrelated container/text values for this command.
        // All candidate labels/actions remain strict, as does uniqueness.
        if !buttonsOnly || role == (kAXButtonRole as String) {
            let actions = actionNames(element)
            result.append(
            AXNode(
                element: element,
                role: role,
                title: stringAttribute(element, kAXTitleAttribute as CFString),
                description: stringAttribute(element, kAXDescriptionAttribute as CFString),
                help: stringAttribute(element, kAXHelpAttribute as CFString),
                identifier: stringAttribute(element, kAXIdentifierAttribute as CFString),
                value: shouldReadValueAttribute(role: role, actions: actions)
                    ? stringAttribute(element, kAXValueAttribute as CFString) : "",
                actions: actions
            )
            )
        }
        let children = childElements(element)
        if depth >= maxDepth && !children.isEmpty {
            fail("Phone.app accessibility tree exceeded depth limit; no action performed", code: 75)
        }
        for child in children.reversed() {
            stack.append((child, depth + 1))
        }
    }
    return result
}

func isPhoneConfirmationButton(_ node: AXNode) -> Bool {
    guard node.role == (kAXButtonRole as String),
          node.actions.contains(kAXPressAction as String) else { return false }
    let text = node.searchableText
    return text.contains("通信音频") || text.contains("communication audio")
}

func escaped(_ text: String) -> String {
    text.replacingOccurrences(of: "\\", with: "\\\\")
        .replacingOccurrences(of: "\"", with: "\\\"")
        .replacingOccurrences(of: "\n", with: "\\n")
}

func printNodes(_ nodes: [AXNode]) {
    for (index, node) in nodes.enumerated() {
        let fields = [
            "index": String(index),
            "role": node.role,
            "title": node.title,
            "description": node.description,
            "help": node.help,
            "identifier": node.identifier,
            "value": node.value,
            "actions": node.actions.joined(separator: ","),
        ]
        let body = fields.map { key, value in
            "\"\(escaped(key))\":\"\(escaped(value))\""
        }.sorted().joined(separator: ",")
        print("{\(body)}")
    }
}

func isCallButton(_ node: AXNode) -> Bool {
    guard node.role == (kAXButtonRole as String),
          node.actions.contains(kAXPressAction as String)
    else {
        return false
    }
    let text = node.searchableText
    let positive = ["呼叫", "拨打", "call", "dial"]
    let negative = ["取消", "关闭", "拒绝", "挂断", "结束通话", "cancel", "close", "decline", "end call"]
    return positive.contains(where: text.contains)
        && !negative.contains(where: text.contains)
}

func isActiveCallNode(_ node: AXNode) -> Bool {
    let text = node.searchableText
    let activeTerms = ["结束通话", "挂断", "end call", "disconnect call"]
    return activeTerms.contains(where: text.contains)
}

func digitsOnly(_ text: String) -> String {
    String(text.filter(\.isNumber))
}

func recentCallAction(_ actions: [String]) -> String? {
    // SwiftUI exposes the row's call action separately from AXPress (which
    // can merely select/open the row). Use the exact returned action name.
    // Never fall back to a coordinate click or a destructive custom action.
    let candidates = actions.filter { action in
        guard let line = action.components(separatedBy: "\n").first,
              line.hasPrefix("Name:") else { return false }
        let name = String(line.dropFirst(5)).trimmingCharacters(in: .whitespaces).lowercased()
        return name == "呼叫" || name == "call"
    }
    return candidates.count == 1 ? candidates[0] : nil
}

func matchesRecentNumber(_ description: String, wanted: String) -> Bool {
    // Compare whole phone-shaped fields, never a substring of all row digits.
    // Preserve the current Chinese domestic display without matching a
    // different country's +number or an extra trailing digit.
    let pattern = #"(?<![0-9+])\+?[0-9][0-9 ()\u00a0\u202f-]{5,}[0-9](?![0-9])"#
    guard let regex = try? NSRegularExpression(pattern: pattern) else { return false }
    let range = NSRange(description.startIndex..<description.endIndex, in: description)
    return regex.matches(in: description, range: range).contains { match in
        guard let fieldRange = Range(match.range, in: description) else { return false }
        let field = String(description[fieldRange])
        let digits = digitsOnly(field)
        if digits == wanted { return true }
        return !field.hasPrefix("+") && wanted.hasPrefix("86") && wanted.count == 13
            && digits.count == 11 && digits.hasPrefix("1") && digits == String(wanted.dropFirst(2))
    }
}

func performRecentCallAction(_ element: AXUIElement) {
    guard let action = recentCallAction(actionNames(element)) else {
        fail("matching row has no unique named call action; no action performed", code: 75)
    }
    checkInspectionBudget()
    let error = AXUIElementPerformAction(element, action as CFString)
    guard error == .success else {
        fail("named recent-call action returned an error (\(error.rawValue)); will not repeat", code: 66)
    }
    // Accessibility accepted one request, not proof of Sending or Active.
    print("recent-call-action-accepted")
}

func findRecentCall(
    _ root: AXUIElement,
    wanted: String,
    maxDepth: Int = 14,
    maxNodes: Int = 800
) -> (element: AXUIElement, description: String)? {
    var stack: [(AXUIElement, Int)] = [(root, 0)]
    var visited = 0
    while let (element, depth) = stack.popLast(), visited < maxNodes {
        visited += 1
        if stringAttribute(element, kAXRoleAttribute as CFString)
            == (kAXButtonRole as String)
        {
            let identifier = stringAttribute(
                element, kAXIdentifierAttribute as CFString
            )
            if identifier.hasPrefix("recentCall") {
                let description = stringAttribute(
                    element, kAXDescriptionAttribute as CFString
                )
                if matchesRecentNumber(description, wanted: wanted) {
                    return (element, description)
                }
            }
        }
        guard depth < maxDepth else { continue }
        for child in childElements(element).reversed() {
            stack.append((child, depth + 1))
        }
    }
    return nil
}

func fail(_ message: String, code: Int32) -> Never {
    FileHandle.standardError.write(Data((message + "\n").utf8))
    exit(code)
}

let commandIndex = CommandLine.arguments.count > 1
    && CommandLine.arguments[1] == "--compiled" ? 2 : 1
guard CommandLine.arguments.count > commandIndex else {
    fail("usage: phone_ax_helper.swift <permission-check|inspect|press-call|press-recent-call|has-phone-confirmation|press-phone-confirmation|recent-signature|is-call-active|call-status> [pid] [number]", code: 64)
}
guard AXIsProcessTrusted() else {
    fail("accessibility permission is not available", code: 77)
}
let command = CommandLine.arguments[commandIndex]
if command == "permission-check" {
    print("trusted")
    exit(0)
}
let pidIndex = commandIndex + 1
guard CommandLine.arguments.count > pidIndex else {
    fail("missing pid", code: 64)
}
guard let rawPID = Int32(CommandLine.arguments[pidIndex]), rawPID > 1 else {
    fail("invalid pid", code: 64)
}

let app = AXUIElementCreateApplication(pid_t(rawPID))
// The system-wide element sets this helper process's default, including child
// elements. Setting only `app` would leave descendant queries unbounded.
checkInspectionBudget()
let nodes = ["press-recent-call", "recent-signature"].contains(command)
    ? [] : collectNodes(app, buttonsOnly:
        ["has-phone-confirmation", "press-phone-confirmation"].contains(command))

switch command {
case "inspect":
    printNodes(nodes)
case "press-call":
    let candidates = nodes.filter(isCallButton)
    guard candidates.count == 1 else {
        printNodes(candidates)
        fail("expected exactly one accessible call button, found \(candidates.count)", code: 65)
    }
    let error = AXUIElementPerformAction(candidates[0].element, kAXPressAction as CFString)
    guard error == .success else {
        fail("failed to press call button: \(error.rawValue)", code: 66)
    }
    print("call-button-pressed")
case "press-recent-call":
    guard CommandLine.arguments.count == pidIndex + 2 else {
        fail("press-recent-call requires a phone number", code: 64)
    }
    let wanted = digitsOnly(CommandLine.arguments[pidIndex + 1])
    guard wanted.count >= 7 else {
        fail("invalid phone number", code: 64)
    }
    guard let candidate = findRecentCall(app, wanted: wanted) else {
        fail("matching recent-call row was not found", code: 65)
    }
    performRecentCallAction(candidate.element)
case "has-phone-confirmation", "press-phone-confirmation":
    let candidates = nodes.filter(isPhoneConfirmationButton)
    guard candidates.count == 1 else {
        printNodes(candidates)
        // Only an empty, fully inspected tree means absence. Multiple buttons
        // are an ambiguous pending call, not permission to create another one.
        fail("expected exactly one Phone.app communication-audio button, found \(candidates.count)",
             code: candidates.isEmpty ? 65 : 75)
    }
    if command == "has-phone-confirmation" {
        print("phone-confirmation-ready")
        exit(0)
    }
    let error = AXUIElementPerformAction(
        candidates[0].element, kAXPressAction as CFString
    )
    guard error == .success else {
        fail("failed to press Phone.app confirmation: \(error.rawValue)", code: 66)
    }
    print("phone-confirmation-pressed")
case "recent-signature":
    guard CommandLine.arguments.count == pidIndex + 2 else {
        fail("recent-signature requires a phone number", code: 64)
    }
    let wanted = digitsOnly(CommandLine.arguments[pidIndex + 1])
    guard wanted.count >= 7 else {
        fail("invalid phone number", code: 64)
    }
    guard let recent = findRecentCall(app, wanted: wanted) else {
        fail("matching recent-call row was not found", code: 65)
    }
    guard recentCallAction(actionNames(recent.element)) != nil else {
        fail("matching row has no unique named call action; no action performed", code: 75)
    }
    print(recent.description)
case "is-call-active":
    if nodes.contains(where: isActiveCallNode) {
        print("active")
        exit(0)
    }
    print("inactive")
    exit(3)
case "call-status":
    if nodes.contains(where: isActiveCallNode) {
        print("active")
        exit(0)
    }
    let failureTerms = [
        "呼叫失败",
        "call failed",
        "若要接打电话",
        "iphone在附近",
        "iphone 在附近",
        "same wi-fi",
        "same wifi",
    ]
    if let failure = nodes.first(where: { node in
        failureTerms.contains(where: node.searchableText.contains)
    }) {
        let detail = [failure.title, failure.description, failure.value]
            .filter { !$0.isEmpty }
            .joined(separator: " ")
        print(detail.isEmpty ? "call failed" : detail)
        exit(4)
    }
    print("pending")
    exit(3)
default:
    fail("unknown command", code: 64)
}
