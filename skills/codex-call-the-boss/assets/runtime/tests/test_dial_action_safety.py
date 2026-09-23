"""No real UI actions: execute Swift selection policy and replay bounded call events."""
from pathlib import Path
import subprocess
import unittest
from datetime import datetime, timedelta

from iphone_audio import IPhoneDialer


class BoundedCallBindingTests(unittest.TestCase):
    def setUp(self):
        self.started = datetime.fromisoformat('2026-09-08T19:07:06.398791+08:00')
        self.own = 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa'
        self.other = 'bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb'

    def event(self, after, state, identity=None):
        return {'timestamp': (self.started + timedelta(seconds=after)).strftime('%Y-%m-%d %H:%M:%S.%f%z'),
                'eventMessage': f'TUCallCenterCallStatusChangedNotification uPI={identity or self.own} stat={state}'}

    def test_failed_unbound_attempt_cannot_adopt_24_minutes_later_call(self):
        # The retained failed job ended at 19:08:08 without a system UUID.
        # Phone.app created a call only at 19:31:24; it must not be back-bound.
        events = [self.event(1458.312142, 'Sending'), self.event(1465.801370, 'Active'),
                  self.event(1477.475968, 'Disconnected')]
        self.assertEqual(IPhoneDialer._state_from_call_events(events, self.started.timestamp()),
                         ('unknown', ''))

    def test_timely_sending_keeps_its_own_later_disconnect(self):
        events = [self.event(1, 'Sending'), self.event(8, 'Active'), self.event(1477, 'Disconnected')]
        self.assertEqual(IPhoneDialer._state_from_call_events(events, self.started.timestamp()),
                         ('disconnected', self.own))

    def test_later_call_does_not_make_timely_candidate_ambiguous(self):
        events = [self.event(1, 'Sending'), self.event(8, 'Active'),
                  self.event(1458, 'Sending', self.other), self.event(1465, 'Disconnected', self.other)]
        self.assertEqual(IPhoneDialer._state_from_call_events(events, self.started.timestamp()),
                         ('active', self.own))

    def test_explicit_bound_call_still_accepts_only_its_own_events(self):
        events = [self.event(8, 'Active'), self.event(1458, 'Sending', self.other),
                  self.event(1465, 'Disconnected', self.other)]
        self.assertEqual(IPhoneDialer._state_from_call_events(events, self.started.timestamp(), self.own),
                         ('active', self.own))

    def test_two_timely_candidates_are_still_ambiguous(self):
        events = [self.event(1, 'Sending'), self.event(2, 'Sending', self.other), self.event(8, 'Active')]
        self.assertEqual(IPhoneDialer._state_from_call_events(events, self.started.timestamp()),
                         ('unknown', ''))


class RecentActionPolicyTests(unittest.TestCase):
    def function(self, name):
        source = Path('phone_ax_helper.swift').read_text()
        start = source.index('func ' + name + '(')
        return source[start:source.index('\n}\n', start) + 3]

    def test_named_action_is_exact_unique_and_never_generic_press_or_delete(self):
        function = self.function('recentCallAction')
        harness = r'''
import Foundation
let call = "Name:呼叫\nTarget:0x0\nSelector:(null)"
let english = "Name:Call\nTarget:0x0\nSelector:(null)"
let deletion = "Name:删除\nTarget:0x0\nSelector:(null)"
precondition(recentCallAction(["AXPress", deletion, call]) == call)
precondition(recentCallAction([english]) == english)
precondition(recentCallAction(["AXPress", deletion]) == nil)
precondition(recentCallAction(["Name:取消呼叫\nTarget:0x0"]) == nil)
precondition(recentCallAction([call, english]) == nil)
precondition(recentCallAction([call, call]) == nil)
print("named-action-policy-passed")
'''
        result = subprocess.run(['/usr/bin/swift', '-'], input=function + harness,
                                capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('named-action-policy-passed', result.stdout)

    def test_whole_number_match_rejects_wrong_country_extra_digit_and_split_fields(self):
        functions = self.function('digitsOnly') + self.function('matchesRecentNumber')
        checks = r'''
let wanted = "8613800138000"
for text in ["呼叫 +86 138 0013 8000，今天", "13800138000，去电", "8613800138000"] {
    precondition(matchesRecentNumber(text, wanted: wanted), text)
}
for text in ["呼叫 +1 380 013 8000", "+86 138001380001", "+13800138000", "1380013，8000", "86138001380001"] {
    precondition(!matchesRecentNumber(text, wanted: wanted), text)
}
precondition(matchesRecentNumber("+1 (415) 555-0100", wanted: "14155550100"))
precondition(!matchesRecentNumber("+86 14155550100", wanted: "14155550100"))
print("exact-number-policy-passed")
'''
        result = subprocess.run(['/usr/bin/swift', '-'], input='import Foundation\n' + functions + checks,
                                capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_production_action_dispatches_once_and_errors_never_fall_back(self):
        functions = self.function('recentCallAction') + self.function('performRecentCallAction')
        for scenario, expected_code, expected_calls in [
            ('ready', 0, 1), ('missing', 75, 0), ('ambiguous', 75, 0),
            ('read_failed', 75, 0), ('budget_expired', 75, 0), ('action_failed', 66, 1),
        ]:
            with self.subTest(scenario=scenario):
                harness = r'''
import Foundation
import CoreFoundation
typealias AXUIElement = Int
struct FakeAXError: Equatable {
    let rawValue: Int
    static let success = FakeAXError(rawValue: 0)
}
let scenario = "SCENARIO"
let call = "Name:呼叫\nTarget:0x0\nSelector:(null)"
var count = 0
func fail(_ message: String, code: Int32) -> Never { print("calls=\(count)"); exit(code) }
func actionNames(_ element: AXUIElement) -> [String] {
    if scenario == "read_failed" { fail("read failed", code: 75) }
    if scenario == "missing" { return ["AXPress", "Name:删除"] }
    if scenario == "ambiguous" { return [call, call] }
    return ["AXPress", "Name:删除", call]
}
func checkInspectionBudget() {
    if scenario == "budget_expired" { fail("budget expired", code: 75) }
}
func AXUIElementPerformAction(_ element: AXUIElement, _ action: CFString) -> FakeAXError {
    precondition(element == 7 && action as String == call)
    count += 1
    return scenario == "action_failed" ? FakeAXError(rawValue: -25204) : .success
}
'''.replace('SCENARIO', scenario)
                result = subprocess.run(['/usr/bin/swift', '-'],
                    input=harness + functions + '\nperformRecentCallAction(7)\nprint("calls=\\(count)")\n',
                    capture_output=True, text=True, timeout=20)
                self.assertEqual(result.returncode, expected_code, result.stderr)
                self.assertIn(f'calls={expected_calls}', result.stdout)


class ConfirmationNodePolicyTests(unittest.TestCase):
    def test_complete_button_search_ignores_only_irrelevant_fields_and_fails_closed(self):
        source = Path('phone_ax_helper.swift').read_text()
        start = source.index('struct AXNode {')
        node_type = source[start:source.index('\n}\n', start) + 3]
        functions = ''.join(RecentActionPolicyTests().function(name) for name in (
            'shouldReadValueAttribute', 'collectNodes', 'isPhoneConfirmationButton'))
        harness = r'''
import Foundation
import CoreFoundation
typealias AXUIElement = Int
let kAXRoleAttribute = "AXRole", kAXTitleAttribute = "AXTitle"
let kAXDescriptionAttribute = "AXDescription", kAXHelpAttribute = "AXHelp"
let kAXIdentifierAttribute = "AXIdentifier", kAXValueAttribute = "AXValue"
let kAXButtonRole = "AXButton", kAXGroupRole = "AXGroup", kAXPressAction = "AXPress"
let scenario = "SCENARIO"
func fail(_ message: String, code: Int32) -> Never { print("actions=0"); exit(code) }
func stringAttribute(_ element: AXUIElement, _ attribute: CFString) -> String {
    if attribute as String == "AXRole" {
        if scenario == "missing_role" && element == 1 { return "" }
        return element == 0 ? "AXApplication" : element == 1 ? "AXGroup" : "AXButton"
    }
    // Every unrelated container field is broken in this fixture. It is never
    // needed for the button-only predicate, but the complete tree is visited.
    if element < 2 { fail("unrelated attribute failed", code: 75) }
    if scenario == "button_read_failed" && attribute as String == "AXValue" {
        fail("required button value failed", code: 75)
    }
    if attribute as String == "AXDescription" {
        return scenario == "container_impostor" ? "unrelated button" : "通信音频"
    }
    return ""
}
func actionNames(_ element: AXUIElement) -> [String] {
    if element < 2 || scenario == "button_actions_failed" { fail("actions failed", code: 75) }
    return ["AXPress"]
}
func childElements(_ element: AXUIElement) -> [AXUIElement] {
    if element == 0 { return [1] }
    if element == 1 {
        if scenario == "children_failed" { fail("children failed", code: 75) }
        return scenario == "ambiguous" ? [2, 3] : [2]
    }
    return []
}
'''
        invocation = r'''
let nodes = collectNodes(0, maxDepth: scenario == "depth_limit" ? 1 : 14,
    maxNodes: scenario == "node_limit" ? 2 : 800, buttonsOnly: scenario != "full_inspection")
let candidates = nodes.filter(isPhoneConfirmationButton)
guard candidates.count == 1 else { fail("not unique", code: candidates.isEmpty ? 65 : 75) }
precondition(candidates[0].element == 2)
print("unique=2 actions=0")
'''
        for scenario, expected in [
            ('ready', 0), ('ambiguous', 75), ('container_impostor', 65),
            ('button_read_failed', 75), ('button_actions_failed', 75),
            ('children_failed', 75), ('missing_role', 75),
            ('node_limit', 75), ('depth_limit', 75), ('full_inspection', 75),
        ]:
            with self.subTest(scenario=scenario):
                result = subprocess.run(['/usr/bin/swift', '-'],
                    input=harness.replace('SCENARIO', scenario) + node_type + functions + invocation,
                    capture_output=True, text=True, timeout=20)
                self.assertEqual(result.returncode, expected, result.stderr)
                self.assertIn('actions=0', result.stdout)


if __name__ == '__main__':
    unittest.main()
