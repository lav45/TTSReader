"""Тесты MessageQueue: сообщения по задачам демона, порядок, навигация, хвост начатых (tail, по умолчанию 5)."""
import tests  # noqa: F401 — окружение тестов (tests/__init__.py)
import logging
import tempfile
import unittest
import wave
from pathlib import Path
from unittest import mock

from tts_player.model import Message, Sentence
from tts_player.queue import MessageQueue, QueueSnapshot, TaskOrder


class MessageQueueTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        self.queue = MessageQueue(tail=5, order=TaskOrder())

    def message(self, task="t1", session="s1"):
        """Сообщение задачи хука, отправленное демону с id task."""
        message = Message(session)
        self.queue.append(message)
        self.queue.link(message, task)
        return message

    def sentence(self, recv_ns, task="t1", index=0, session="s1", token="aa"):
        path = self.dir / f"{task}-{session}-{token}-{index}-{recv_ns}.wav"
        path.write_bytes(b"wav")
        return Sentence(recv_ns, task, session, token, index, path)

    def add(self, recv_ns, task="t1", index=0, session="s1", token="aa"):
        """Предложение задачи task; сообщения для task ещё нет — оно заводится первым."""
        if not any(task in m.task_ids for m in self.queue.messages):
            self.message(task, session)
        sentence = self.sentence(recv_ns, task, index, session, token)
        self.queue.add(sentence)
        return sentence

    def tasks(self):
        return [self.queue.task_of(m) for m in self.queue.messages]

    def test_sentences_of_one_task_make_one_message(self):
        a, b = self.add(1, index=0), self.add(2, index=1)
        self.assertEqual(1, len(self.queue.messages))
        self.assertEqual([a, b], self.queue.messages[0].sentences)

    def test_other_task_is_other_message(self):
        self.add(1, task="t1")
        self.add(2, task="t2")
        self.assertEqual(["t1", "t2"], self.tasks())

    def test_two_passes_of_one_task_make_one_message_by_recv_ns(self):
        """Два прохода синтеза одной задачи (новый token) — одно сообщение: пауза демона оборвала
        проход aa на предложении 1, проход bb начат с него же; дубль индекса 1 — поздний проход
        (больший recv_ns), в каком бы порядке файлы ни пришли."""
        message = self.message("f00d")
        a0 = self.sentence(1, "f00d", 0, token="aa")
        a1 = self.sentence(2, "f00d", 1, token="aa")
        b1 = self.sentence(4, "f00d", 1, token="bb")
        b2 = self.sentence(5, "f00d", 2, token="bb")
        for s in (a0, b1, a1, b2):
            self.queue.add(s)
        self.assertEqual([message], self.queue.messages)
        self.assertEqual([a0, b1, b2], message.sentences)
        self.assertFalse(a1.path.exists())

    def test_duplicate_index_of_later_pass_replaces_unsounded(self):
        message = self.message("f00d")
        a0, a1 = self.sentence(1, "f00d", 0), self.sentence(2, "f00d", 1)
        for s in (a0, a1):
            self.queue.add(s)
        b1 = self.sentence(4, "f00d", 1, token="bb")
        self.queue.add(b1, sounded=a0)
        self.assertEqual([a0, b1], message.sentences)
        self.assertFalse(a1.path.exists())

    def test_duplicate_index_of_sounded_sentence_is_deleted_not_added(self):
        message = self.message("f00d")
        a0, a1 = self.sentence(1, "f00d", 0), self.sentence(2, "f00d", 1)
        for s in (a0, a1):
            self.queue.add(s)
        b1 = self.sentence(4, "f00d", 1, token="bb")
        self.queue.add(b1, sounded=a1)
        self.assertEqual([a0, a1], message.sentences)
        self.assertFalse(b1.path.exists())
        self.assertTrue(a1.path.exists())

    def test_message_is_in_queue_before_its_sentences(self):
        message = self.message("t1")
        self.assertEqual([message], self.queue.messages)
        self.assertEqual([], message.sentences)
        self.assertEqual(0.0, self.queue.length(message))

    def test_sentence_of_any_task_of_message_joins_it(self):
        message = self.message("t1")
        self.queue.link(message, "t2")
        sentence = self.sentence(1, "t2")
        self.queue.add(sentence)
        self.assertEqual([sentence], message.sentences)
        self.assertEqual({"t1", "t2"}, message.task_ids)
        self.assertEqual("t2", self.queue.task_of(message))

    def test_task_of_message_without_tasks_is_none(self):
        message = Message("s1", sent=False)
        self.queue.append(message)
        self.assertIsNone(self.queue.task_of(message))

    def test_unknown_task_sentence_is_deleted_with_warning(self):
        self.message("t1")
        late = self.sentence(1, "unknown")
        with self.assertLogs("tts_player.queue", "WARNING") as logs:
            self.queue.add(late)
        self.assertFalse(late.path.exists())
        self.assertIn(late.path.name, logs.output[0])
        self.assertEqual([], self.queue.messages[0].sentences)

    def test_foreign_task_sentence_is_deleted_with_warning(self):
        late = self.sentence(1, "beef")
        with self.assertLogs("tts_player.queue", "WARNING"):
            self.queue.add(late)
        self.assertFalse(late.path.exists())
        self.assertEqual([], self.queue.messages)

    def test_sentences_ordered_by_recv_ns(self):
        message = self.message()
        a, b, c = (self.sentence(n, index=n) for n in (1, 2, 3))
        for s in (c, a, b):
            self.queue.add(s)
        self.assertEqual([a, b, c], message.sentences)

    def test_messages_ordered_by_arrival_not_by_sentences(self):
        """Порядок — приход задач хука: переозвучка не двигает сообщение в списке."""
        first, second = self.message("t1"), self.message("t2")
        self.queue.add(self.sentence(5, "t1"))
        self.queue.add(self.sentence(3, "t2"))
        self.assertEqual([first, second], self.queue.messages)

    def test_messages_is_a_copy(self):
        self.add(1)
        self.queue.messages.clear()
        self.assertEqual(1, len(self.queue.messages))

    def test_empty_queue(self):
        self.assertEqual([], self.queue.messages)

    def test_following_walks_messages(self):
        for n in (1, 2, 3):
            self.add(n, task=f"t{n}")
        first, second, third = self.queue.messages
        self.assertIs(second, self.queue.following(first))
        self.assertIs(third, self.queue.following(second))
        self.assertIsNone(self.queue.following(third))

    def test_new_message_is_not_heard(self):
        self.add(1)
        self.assertFalse(self.queue.messages[0].heard)

    def test_is_complete_false_while_its_task_is_last_delivered(self):
        self.add(1)
        self.assertFalse(self.queue.is_complete(self.queue.messages[0]))

    def test_is_complete_false_for_message_without_sentences_yet(self):
        first = self.message("t1")
        self.message("t2")
        self.assertFalse(self.queue.is_complete(first))

    def test_is_complete_when_later_task_delivers(self):
        """Демон синтезирует задачи по одной: предложение более поздней задачи завершает
        все сообщения, чьи задачи отправлены раньше, — даже без своих предложений."""
        first, second = self.message("t1"), self.message("t2")
        third = self.message("t3")
        self.queue.add(self.sentence(1, "t2"))
        self.assertEqual([True, False, False],
                         [self.queue.is_complete(m) for m in (first, second, third)])

    def test_later_task_of_earlier_message_is_not_complete(self):
        """Переозвучка: новая задача первого сообщения — после задачи второго."""
        first, second = self.message("t1"), self.message("t2")
        self.queue.add(self.sentence(1, "t1"))
        self.queue.link(first, "t3")
        self.queue.add(self.sentence(2, "t2"))
        self.assertFalse(self.queue.is_complete(first))
        self.assertFalse(self.queue.is_complete(second))
        self.queue.add(self.sentence(3, "t3"))
        self.assertTrue(self.queue.is_complete(second))

    def test_not_sent_message_is_complete(self):
        """Задача не ушла в демон: ждать нечего."""
        message = Message("s1", sent=False)
        self.queue.append(message)
        self.assertTrue(self.queue.is_complete(message))

    def test_is_complete_when_last_task_covers_all_indices(self):
        """Число предложений известно: сообщение завершено, когда у его последней задачи
        есть все индексы 0…n−1, — без предложения следующей задачи."""
        message = self.message("t1")
        message.sentence_count = 3
        self.queue.add(self.sentence(1, "t1", 0))
        self.queue.add(self.sentence(2, "t1", 2))
        self.assertFalse(self.queue.is_complete(message))
        self.queue.add(self.sentence(3, "t1", 1))
        self.assertTrue(self.queue.is_complete(message))

    def test_is_complete_by_indices_of_two_passes_with_duplicate(self):
        """Пауза демона: проход bb начат с оборванного предложения 1 — дубль индекса 1 не
        мешает, индексы считаются по всем проходам задачи."""
        message = self.message("t1")
        message.sentence_count = 3
        for recv_ns, index, token in ((1, 0, "aa"), (2, 1, "aa"), (4, 1, "bb")):
            self.queue.add(self.sentence(recv_ns, "t1", index, token=token))
        self.assertFalse(self.queue.is_complete(message))
        self.queue.add(self.sentence(5, "t1", 2, token="bb"))
        self.assertTrue(self.queue.is_complete(message))

    def test_zero_sentences_is_complete_at_once(self):
        """Пустой текст: демон не синтезирует ничего, ждать нечего."""
        message = self.message("t1")
        message.sentence_count = 0
        self.assertTrue(self.queue.is_complete(message))

    def test_indices_count_only_for_last_task(self):
        """Переозвучка: предложения прежней задачи новую не завершают."""
        message = self.message("t1")
        message.sentence_count = 1
        self.queue.add(self.sentence(1, "t1", 0))
        self.queue.link(message, "t2")
        self.assertFalse(self.queue.is_complete(message))
        self.queue.add(self.sentence(2, "t2", 0))
        self.assertTrue(self.queue.is_complete(message))

    def test_unknown_count_keeps_old_signals(self):
        """Счётчик недоступен (None): все индексы ещё не конец, конец — по прежним признакам."""
        message = self.message("t1")
        self.queue.add(self.sentence(1, "t1", 0))
        self.assertFalse(self.queue.is_complete(message))
        self.add(2, task="t2")
        self.assertTrue(self.queue.is_complete(message))

    def test_late_sentence_of_started_message_is_added(self):
        a = self.add(1, index=0)
        message = self.queue.messages[0]
        self.queue.start(message)
        b = self.add(2, index=1)
        self.assertEqual([a, b], message.sentences)

    def test_sentence_of_heard_message_is_deleted_not_added(self):
        self.add(1, task="t1", index=0)
        message = self.queue.messages[0]
        self.queue.start(message)
        self.queue.mark_heard(message)
        late = self.sentence(3, task="t1", index=1)
        with self.assertLogs("tts_player.queue", "INFO"):
            self.queue.add(late)
        self.assertEqual(1, len(message.sentences))
        self.assertFalse(late.path.exists())

    def test_forget_audio_deletes_files_and_resets_message(self):
        message = self.message()
        sentences = [self.add(n, index=n) for n in (1, 2)]
        message.heard = True
        self.queue.forget_audio(message)
        self.assertFalse(any(s.path.exists() for s in sentences))
        self.assertEqual(([], False), (message.sentences, message.heard))
        self.assertEqual([message], self.queue.messages)

    def start_messages(self, count):
        sentences = [self.add(n, task=f"t{n}") for n in range(1, count + 1)]
        for message in self.queue.messages:
            self.queue.start(message)
        return sentences

    def test_sixth_started_message_deletes_first_only(self):
        sentences = [self.add(n, task=f"t{n}") for n in range(1, 7)]
        messages = self.queue.messages
        for message in messages[:5]:
            self.queue.start(message)
        self.assertTrue(all(s.path.exists() for s in sentences))
        self.queue.start(messages[5])
        self.assertFalse(sentences[0].path.exists())
        self.assertTrue(all(s.path.exists() for s in sentences[1:]))
        self.assertEqual(messages[1:], self.queue.messages)

    def start_task(self, task):
        self.queue.start(next(m for m in self.queue.messages if task in m.task_ids))

    def test_is_started_only_after_start(self):
        self.add(1, task="t1")
        message = self.queue.messages[0]
        self.assertFalse(self.queue.is_started(message))
        self.queue.start(message)
        self.assertTrue(self.queue.is_started(message))

    def test_evicted_message_is_not_started(self):
        self.queue.tail = 1
        self.start_messages(2)
        first = self.queue.messages[0]
        self.assertTrue(self.queue.is_started(first))
        self.add(9, task="t9")
        evicted = first
        self.start_task("t9")
        self.assertFalse(self.queue.is_started(evicted))

    def test_smaller_tail_shrinks_by_one_per_start(self):
        """tail 5 → 2 при пяти начатых: каждый start уменьшает хвост на одно сообщение,
        а не обрезает сразу до двух; через три новых начатых их 2."""
        self.start_messages(5)
        self.queue.tail = 2
        for n in range(6, 9):
            self.add(n, task=f"t{n}")
        self.start_task("t6")
        self.assertEqual(["t3", "t4", "t5", "t6", "t7", "t8"], self.tasks())
        self.start_task("t7")
        self.assertEqual(["t5", "t6", "t7", "t8"], self.tasks())
        self.start_task("t8")
        self.assertEqual(["t7", "t8"], self.tasks())

    def test_shrunk_tail_then_holds_its_size(self):
        self.start_messages(5)
        self.queue.tail = 2
        for n in range(6, 10):
            self.add(n, task=f"t{n}")
            self.queue.start(self.queue.messages[-1])
        self.assertEqual(["t8", "t9"], self.tasks())

    def test_larger_tail_stops_evicting_until_new_limit(self):
        """tail 2 → 5: новые начатые не вытесняют, пока их меньше 5."""
        self.queue.tail = 2
        self.start_messages(2)
        self.queue.tail = 5
        for n in range(3, 6):
            self.add(n, task=f"t{n}")
            self.queue.start(self.queue.messages[-1])
        self.assertEqual(["t1", "t2", "t3", "t4", "t5"], self.tasks())
        self.add(6, task="t6")
        self.queue.start(self.queue.messages[-1])
        self.assertEqual(["t2", "t3", "t4", "t5", "t6"], self.tasks())

    def test_restarting_started_message_does_not_evict(self):
        sentences = self.start_messages(5)
        self.queue.start(self.queue.messages[0])
        self.assertTrue(all(s.path.exists() for s in sentences))

    def test_deleted_message_leaves_queue_and_missing_file_is_fine(self):
        sentences = [self.add(n, task=f"t{n}") for n in range(1, 8)]
        sentences[1].path.unlink()
        for message in self.queue.messages:
            self.queue.start(message)
        self.assertFalse(any(s.path.exists() for s in sentences[:2]))
        self.assertTrue(all(s.path.exists() for s in sentences[2:]))
        self.assertEqual(5, len(self.queue.messages))

    def test_all_sentences_of_oldest_message_deleted(self):
        first = [self.add(1, index=0), self.add(2, index=1)]
        for n in range(3, 8):
            self.add(n, task=f"t{n}")
        for message in self.queue.messages:
            self.queue.start(message)
        self.assertFalse(any(s.path.exists() for s in first))

    def test_unstarted_messages_never_deleted(self):
        sentences = [self.add(n, task=f"t{n}") for n in range(1, 21)]
        self.queue.start(self.queue.messages[0])
        self.assertTrue(all(s.path.exists() for s in sentences))
        self.assertEqual(20, len(self.queue.messages))

    def test_unlink_failure_on_eviction_does_not_break_queue(self):
        with mock.patch.object(Path, "unlink", side_effect=PermissionError("denied")):
            with self.assertLogs("tts_player.queue", "ERROR"):
                self.start_messages(7)
        self.assertEqual(["t3", "t4", "t5", "t6", "t7"], self.tasks())

    def test_sentence_of_evicted_message_is_deleted_not_added(self):
        self.start_messages(6)
        late = self.sentence(100, task="t1", index=1)
        with self.assertLogs("tts_player.queue", "WARNING"):
            self.queue.add(late)
        self.assertFalse(late.path.exists())
        self.assertNotIn("t1", self.tasks())

    def deferred(self, text="отложено"):
        """Отложенное сообщение: только текст, ни задачи, ни файлов."""
        message = Message("s1", text=text, sent=False, deferred=True)
        self.queue.append(message)
        return message

    def test_trim_keeps_last_tail_messages_of_any_kind(self):
        """Режутся самые старые: начатые и нет, прослушанные, отложенные; их файлы удалены."""
        self.queue.tail = 2
        started, unheard = self.add(1, task="t1"), self.add(2, task="t2")
        self.queue.start(self.queue.messages[0])
        self.queue.mark_heard(self.queue.messages[0])
        first_deferred = self.deferred("d1")
        last = self.add(4, task="t4")
        self.queue.trim()
        self.assertEqual(["t4"], [self.queue.task_of(m) for m in self.queue.messages
                                  if m.task_ids])
        self.assertEqual(2, len(self.queue.messages))
        self.assertIs(first_deferred, self.queue.messages[0])
        self.assertFalse(started.path.exists())
        self.assertFalse(unheard.path.exists())
        self.assertTrue(last.path.exists())

    def test_trim_forgets_dropped_started_messages(self):
        self.queue.tail = 1
        self.add(1, task="t1")
        first = self.queue.messages[0]
        self.queue.start(first)
        self.add(2, task="t2")
        self.queue.trim()
        self.assertFalse(self.queue.is_started(first))
        self.assertEqual(["t2"], self.tasks())

    def test_trim_drops_deferred_texts_without_files(self):
        self.queue.tail = 1
        self.deferred("d1")
        last = self.deferred("d2")
        self.queue.trim()
        self.assertEqual([last], self.queue.messages)

    def test_trim_keeps_old_kept_message_beyond_tail(self):
        """keep (текущее) места в tail не занимает: tail последних плюс он, если он старше."""
        self.queue.tail = 2
        sentences = [self.add(n, task=f"t{n}") for n in range(1, 5)]
        kept = self.queue.messages[0]
        self.queue.trim(keep=kept)
        self.assertEqual(["t1", "t3", "t4"], self.tasks())
        self.assertEqual([True, False, True, True], [s.path.exists() for s in sentences])

    def test_trim_with_kept_among_newest_keeps_tail(self):
        self.queue.tail = 2
        [self.add(n, task=f"t{n}") for n in range(1, 5)]
        self.queue.trim(keep=self.queue.messages[-1])
        self.assertEqual(["t3", "t4"], self.tasks())

    def test_trim_tail_one_keeps_new_message_after_old_kept(self):
        """tail 1, текущее доиграно, пришло новое: остаются оба — новое не уступает текущему."""
        self.queue.tail = 1
        self.add(1, task="t1")
        kept = self.queue.messages[0]
        self.message("t2")
        self.queue.trim(keep=kept)
        self.assertEqual(["t1", "t2"], self.tasks())

    def test_trim_within_tail_changes_nothing(self):
        sentences = [self.add(n, task=f"t{n}") for n in range(1, 6)]
        self.queue.trim()
        self.assertEqual(["t1", "t2", "t3", "t4", "t5"], self.tasks())
        self.assertTrue(all(s.path.exists() for s in sentences))

    def test_sentence_of_trimmed_message_is_deleted_not_added(self):
        self.queue.tail = 1
        self.add(1, task="t1")
        self.add(2, task="t2")
        self.queue.trim()
        late = self.sentence(100, task="t1", index=1)
        with self.assertLogs("tts_player.queue", "WARNING"):
            self.queue.add(late)
        self.assertFalse(late.path.exists())

    def test_sentence_of_evicted_message_with_missing_file_is_fine(self):
        self.start_messages(6)
        late = self.sentence(100, task="t1", index=1)
        late.path.unlink()
        with self.assertLogs("tts_player.queue", "WARNING"):
            self.queue.add(late)
        self.assertNotIn("t1", self.tasks())

    def test_sentence_of_evicted_message_unlink_error_is_logged(self):
        self.start_messages(6)
        late = self.sentence(100, task="t1", index=1)
        with mock.patch.object(Path, "unlink", side_effect=PermissionError("denied")):
            with self.assertLogs("tts_player.queue", "ERROR") as logs:
                self.queue.add(late)
        self.assertTrue(any(str(late.path) in line for line in logs.output))
        self.assertNotIn("t1", self.tasks())

    def test_messages_with_same_fields_are_different_messages(self):
        self.assertNotEqual(Message("s1"), Message("s1"))

    def test_clear_deletes_all_files_and_forgets(self):
        sentences = [self.add(n, task=f"t{n}") for n in range(1, 4)]
        self.queue.start(self.queue.messages[0])
        self.queue.mark_heard(self.queue.messages[0])
        self.queue.clear()
        self.assertEqual([], self.queue.messages)
        self.assertFalse(any(s.path.exists() for s in sentences))

    def test_sentence_of_cleared_message_is_deleted_not_added(self):
        self.add(1, task="t1", index=0)
        self.queue.clear()
        late = self.sentence(2, task="t1", index=1)
        with self.assertLogs("tts_player.queue", "WARNING"):
            self.queue.add(late)
        self.assertFalse(late.path.exists())
        self.assertEqual([], self.queue.messages)

    def test_new_message_after_clear_is_added(self):
        self.add(1, task="t1")
        self.queue.clear()
        fresh = self.add(2, task="t2")
        self.assertEqual([[fresh]], [m.sentences for m in self.queue.messages])

    def test_start_of_forgotten_message_is_noop(self):
        self.add(1, task="t1")
        message = self.queue.messages[0]
        self.queue.clear()
        with self.assertLogs("tts_player.queue", "DEBUG"):
            self.queue.start(message)
        self.assertEqual([], self.queue.messages)
        self.start_messages(6)
        self.assertEqual(5, len(self.queue.messages))



class SnapshotTest(unittest.TestCase):
    """Выход и старт плеера: release() отдаёт снимок очереди, restore() его принимает."""

    setUp = MessageQueueTest.setUp
    message = MessageQueueTest.message
    sentence = MessageQueueTest.sentence
    add = MessageQueueTest.add
    tasks = MessageQueueTest.tasks

    def test_release_keeps_unheard_files_and_deletes_heard(self):
        heard = self.add(1, task="t1")
        unheard = self.add(2, task="t2")
        first, second = self.queue.messages
        self.queue.start(first)
        self.queue.mark_heard(first)
        snapshot = self.queue.release()
        self.assertEqual([first, second], snapshot.messages)
        self.assertEqual([first], snapshot.started)
        self.assertFalse(heard.path.exists())
        self.assertEqual([], first.sentences)
        self.assertTrue(unheard.path.exists())
        self.assertEqual([unheard], second.sentences)
        self.assertEqual([], self.queue.messages)

    def test_release_keeps_task_order_and_delivered(self):
        self.add(1, task="t1")
        self.message("t2")
        snapshot = self.queue.release()
        self.assertEqual({"t1": 1, "t2": 2}, snapshot.task_order)
        self.assertEqual(1, snapshot.delivered)

    def test_restore_round_trip_keeps_completeness_and_takes_late_sentences(self):
        """Предложения задачи, поставленной до выхода, после старта — её сообщения."""
        self.add(1, task="t1")
        self.message("t2")
        self.add(2, task="t3")
        snapshot = self.queue.release()
        queue = MessageQueue(tail=5, order=TaskOrder())
        queue.restore(snapshot)
        first, second, third = queue.messages
        self.assertEqual([True, True, False], [queue.is_complete(m) for m in queue.messages])
        late = self.sentence(9, task="t3", index=1)
        queue.add(late)
        self.assertEqual(2, len(third.sentences))
        fresh = Message("s1")
        queue.append(fresh)
        queue.link(fresh, "t4")
        self.assertEqual("t4", queue.task_of(fresh))
        self.assertFalse(queue.is_complete(third))
        queue.add(self.sentence(10, task="t4"))
        self.assertTrue(queue.is_complete(third))

    def test_restore_goes_before_messages_already_queued(self):
        self.add(1, task="t1")
        snapshot = self.queue.release()
        queue = MessageQueue(tail=5, order=TaskOrder())
        newer = Message("s1")
        queue.append(newer)
        queue.link(newer, "t9")
        queue.restore(snapshot)
        self.assertEqual(["t1", "t9"], [queue.task_of(m) for m in queue.messages])

    def test_restore_applies_tail_to_started(self):
        sentences = [self.add(n, task=f"t{n}") for n in range(1, 4)]
        for message in self.queue.messages:
            self.queue.start(message)
        snapshot = self.queue.release()
        queue = MessageQueue(tail=2, order=TaskOrder())
        queue.restore(snapshot)
        self.assertEqual(["t2", "t3"], [queue.task_of(m) for m in queue.messages])
        self.assertFalse(sentences[0].path.exists())
        self.assertTrue(all(queue.is_started(m) for m in queue.messages))

    def test_new_task_after_restore_of_cleared_queue_is_not_complete(self):
        """Очистка забыла задачи, а delivered остался: порядок новых задач идёт после него."""
        self.add(1, task="t1")
        self.add(2, task="t2")
        self.queue.clear()
        snapshot = self.queue.release()
        self.assertEqual(({}, 2), (snapshot.task_order, snapshot.delivered))
        queue = MessageQueue(tail=5, order=TaskOrder())
        queue.restore(snapshot)
        fresh = Message("s1")
        queue.append(fresh)
        queue.link(fresh, "t3")
        queue.add(self.sentence(3, task="t3"))
        self.assertFalse(queue.is_complete(fresh))

    def test_release_drops_finished_sentences_of_current(self):
        """Позиция текущего непрослушанного: кончившиеся до неё предложения удаляются —
        следующий запуск продолжит с недоигранного."""
        message = self.message("t1")
        first, second, third = (self.sentence(n, "t1", n) for n in (1, 2, 3))
        for sentence in (first, second, third):
            self.queue.add(sentence)
        with mock.patch.object(MessageQueue, "_duration", return_value=1.0):
            snapshot = self.queue.release(message, 1.5)
        self.assertEqual([second, third], snapshot.messages[0].sentences)
        self.assertFalse(first.path.exists())
        self.assertTrue(second.path.exists())

    def test_release_keeps_completeness_by_count_of_dropped_sentences(self):
        """Выход посреди сообщения: доигранные предложения удалены, но пришли — после
        перезапуска сообщение завершено по числу предложений."""
        message = self.message("t1")
        message.sentence_count = 2
        self.queue.add(self.sentence(1, "t1", 0))
        self.queue.add(self.sentence(2, "t1", 1))
        with mock.patch.object(MessageQueue, "_duration", return_value=1.0):
            snapshot = self.queue.release(message, 1.5)
        queue = MessageQueue(tail=5, order=TaskOrder())
        queue.restore(snapshot)
        [restored] = queue.messages
        self.assertEqual([1], [s.index for s in restored.sentences])
        self.assertTrue(queue.is_complete(restored))

    def test_forget_audio_forgets_played_indices(self):
        message = self.message("t1")
        message.sentence_count = 2
        self.queue.add(self.sentence(1, "t1", 0))
        self.queue.add(self.sentence(2, "t1", 1))
        with mock.patch.object(MessageQueue, "_duration", return_value=1.0):
            self.queue.release(message, 1.5)
        self.queue.restore(QueueSnapshot([message], [], {"t1": 1}, 1))
        self.queue.forget_audio(message)
        self.queue.link(message, "t2")
        self.queue.add(self.sentence(3, "t2", 1))
        self.assertFalse(self.queue.is_complete(message))

    def test_release_keeps_current_at_sentence_boundary_from_next(self):
        message = self.message("t1")
        first, second = self.sentence(1, "t1", 0), self.sentence(2, "t1", 1)
        self.queue.add(first)
        self.queue.add(second)
        with mock.patch.object(MessageQueue, "_duration", return_value=1.0):
            self.queue.release(message, 1.0)
        self.assertEqual([second], message.sentences)

    def test_release_at_zero_keeps_current_whole(self):
        message = self.message("t1")
        sentence = self.add(1, task="t1")
        self.queue.release(message, 0.0)
        self.assertEqual([sentence], message.sentences)
        self.assertTrue(sentence.path.exists())

    def test_empty_snapshot_restores_nothing(self):
        self.queue.restore(QueueSnapshot([], [], {}, 0))
        self.assertEqual([], self.queue.messages)


class SharedTaskOrderTest(unittest.TestCase):
    """Очереди сессий делят один порядок задач демона: он синтезирует их по одной."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        self.order = TaskOrder()
        self.queue_a = MessageQueue(tail=5, order=self.order)
        self.queue_b = MessageQueue(tail=5, order=self.order)

    def message(self, queue, task, session):
        message = Message(session)
        queue.append(message)
        queue.link(message, task)
        return message

    def offer(self, queue, task, session):
        path = self.dir / f"{task}.wav"
        path.write_bytes(b"wav")
        queue.add(Sentence(1, task, session, "aa", 0, path))

    def test_offer_of_later_task_in_other_queue_completes_earlier_message(self):
        a = self.message(self.queue_a, "a", "sa")
        b = self.message(self.queue_b, "b", "sb")
        self.offer(self.queue_a, "a", "sa")
        self.assertFalse(self.queue_a.is_complete(a))
        self.offer(self.queue_b, "b", "sb")
        self.assertTrue(self.queue_a.is_complete(a))
        self.assertFalse(self.queue_b.is_complete(b))

    def test_offer_of_earlier_task_does_not_complete_later_message(self):
        self.message(self.queue_a, "a", "sa")
        b = self.message(self.queue_b, "b", "sb")
        self.offer(self.queue_a, "a", "sa")
        self.assertFalse(self.queue_b.is_complete(b))

    def test_owns_answers_for_tasks_of_live_messages(self):
        self.message(self.queue_a, "a", "sa")
        self.message(self.queue_b, "b", "sb")
        self.assertTrue(self.queue_a.owns("a"))
        self.assertFalse(self.queue_a.owns("b"))
        self.queue_a.clear()
        self.assertFalse(self.queue_a.owns("a"))

    def test_restore_of_two_snapshots_puts_new_task_after_all(self):
        self.message(self.queue_a, "a", "sa")
        self.message(self.queue_b, "b", "sb")
        self.offer(self.queue_a, "a", "sa")
        self.offer(self.queue_b, "b", "sb")
        snapshots = [self.queue_a.release(), self.queue_b.release()]
        order = TaskOrder()
        queue_a = MessageQueue(tail=5, order=order)
        queue_b = MessageQueue(tail=5, order=order)
        queue_a.restore(snapshots[0])
        queue_b.restore(snapshots[1])
        fresh = self.message(queue_a, "c", "sa")
        self.offer(queue_a, "c", "sa")
        self.assertFalse(queue_a.is_complete(fresh))
        self.assertTrue(queue_a.is_complete(queue_a.messages[0]))
        self.assertGreater(order.order("c"), order.order("a"))
        self.assertGreater(order.order("c"), order.order("b"))
        self.assertTrue(queue_b.is_complete(queue_b.messages[0]))


class LengthTest(unittest.TestCase):
    """Длина сообщения и карта позиций по длительностям WAV предложений."""
    RATE = 1000

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        self.queue = MessageQueue(tail=5, order=TaskOrder())
        self.message = Message("s1")
        self.queue.append(self.message)
        self.queue.link(self.message, "t1")

    def add(self, recv_ns, seconds, index=0):
        path = self.dir / f"t1-s1-aa-{index}-{recv_ns}.wav"
        with wave.open(str(path), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(self.RATE)
            w.writeframes(b"\0\0" * int(seconds * self.RATE))
        sentence = Sentence(recv_ns, "t1", "s1", "aa", index, path)
        self.queue.add(sentence)
        return sentence

    def test_unreadable_sentence_counts_zero_with_warning(self):
        sentence = self.add(1_000_000_000, 1.0)
        sentence.path.write_bytes(b"garbage")
        with self.assertLogs("tts_player.queue", logging.WARNING):
            self.assertEqual(0, self.queue.length(self.message))
        self.assertEqual(0, self.queue.offset_of(self.message, sentence))

    def test_locate_maps_into_sentence(self):
        self.add(1_000_000_000, 1.0, index=0)
        second = self.add(2_000_000_000, 2.0, index=1)
        path, offset = self.queue.locate(self.message, 1.5)
        self.assertEqual(second.path, path)
        self.assertAlmostEqual(0.5, offset)
        self.assertAlmostEqual(1.0, self.queue.offset_of(self.message, second))

    def test_locate_clamps(self):
        first = self.add(1_000_000_000, 1.0, index=0)
        second = self.add(2_000_000_000, 2.0, index=1)
        self.assertEqual((first.path, 0), self.queue.locate(self.message, -3))
        path, offset = self.queue.locate(self.message, 99)
        self.assertEqual(second.path, path)
        self.assertAlmostEqual(2.0, offset)

    def test_locate_finds_zero_length_sentence_at_position(self):
        first = self.add(1_000_000_000, 0, index=0)
        self.add(2_000_000_000, 1.0, index=1)
        middle = self.add(3_000_000_000, 0, index=2)
        self.assertEqual((first.path, 0), self.queue.locate(self.message, 0))
        self.assertEqual((middle.path, 0), self.queue.locate(self.message, 1.0))

    def test_duration_read_once(self):
        self.add(1_000_000_000, 1.0)
        self.queue.locate(self.message, 0.5)
        with mock.patch("tts_player.queue.wave.open", side_effect=AssertionError):
            self.queue.locate(self.message, 0.5)

    def test_length_is_sum_of_received(self):
        self.add(1_000_000_000, 1.0, index=0)
        self.assertAlmostEqual(1.0, self.queue.length(self.message))
        self.add(2_000_000_000, 0.5, index=1)
        self.assertAlmostEqual(1.5, self.queue.length(self.message))


if __name__ == "__main__":
    unittest.main()
