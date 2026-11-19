"""Ticket lint and review must agree on the acceptance text."""
import unittest

from sfc_lib.common import Halt
from sfc_lib.review_policy import criteria
from sfc_lib.ticket_validation import lint_card
from sfc_lib.verification import digest


class AcceptanceCriteriaTests(unittest.TestCase):
    def test_populated_criteria_ignore_empty_placeholders_without_borrowing_text(self):
        body = ('## Acceptance criteria\r\n'
                '- [ ]\r\nIntroductory paragraph\r\n'
                '- [x] first\r\n  continuation\r\n'
                '* [X]\r\nThis paragraph must not revive the empty item.\r\n'
                '* X\r\n'
                '## Notes\r\n- unrelated')
        card = {'body': body}
        self.assertEqual(lint_card(card), [])
        self.assertEqual(criteria(card), [
            {'id': digest([0, 'first\ncontinuation'])[:16], 'text': 'first\ncontinuation'},
            {'id': digest([1, 'X'])[:16], 'text': 'X'},
        ])

    def test_bare_level_two_heading_ends_acceptance_section(self):
        for newline in ('\n', '\r\n'):
            for separator in ('##', '## \t', '## ##', '## Notes'):
                with self.subTest(newline=newline, separator=separator):
                    heading = '## Acceptance criteria'
                    valid = {'body': newline.join((heading, '- First', separator, '- Unrelated'))}
                    self.assertEqual(lint_card(valid), [])
                    self.assertEqual(criteria(valid), [{'id': digest([0, 'First'])[:16], 'text': 'First'}])

                    empty = {'body': newline.join((heading, '- [ ]', separator, '- Unrelated'))}
                    self.assertEqual(lint_card(empty), ['acceptance criteria need observable bullet points'])
                    with self.assertRaisesRegex(Halt, 'observable bullet points'):
                        criteria(empty)

                    duplicate = {'body': newline.join((valid['body'], heading, '- Duplicate'))}
                    self.assertEqual(lint_card(duplicate), ['Use one ## Acceptance criteria section'])
                    with self.assertRaisesRegex(Halt, 'one ## Acceptance criteria'):
                        criteria(duplicate)

    def test_incomplete_and_duplicate_sections_fail_both_paths(self):
        for body, problem in (
            ('##\nAcceptance criteria\n- An item', 'missing ## Acceptance criteria'),
            ('## Acceptance criteria\n- [ ]\nA separate paragraph', 'observable bullet points'),
            ('## Acceptance criteria\n-\n## Notes\n- A different item', 'observable bullet points'),
            ('## Acceptance criteria\n- First\n## ACCEPTANCE CRITERIA\n- Second', 'one ## Acceptance criteria'),
        ):
            with self.subTest(body=body):
                card = {'body': body}
                self.assertEqual(len(lint_card(card)), 1)
                self.assertIn(problem, lint_card(card)[0])
                with self.assertRaisesRegex(Halt, problem):
                    criteria(card)
