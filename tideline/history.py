"""Undo history: a list of whole-project snapshots with a cursor."""


class History:
    def __init__(self, limit=150):
        self.limit = limit
        self.states = []         # [(label, snapshot dict)]
        self.index = -1

    def reset(self, snapshot, label="Open"):
        self.states = [(label, snapshot)]
        self.index = 0

    def push(self, label, snapshot):
        del self.states[self.index + 1:]
        self.states.append((label, snapshot))
        if len(self.states) > self.limit:
            del self.states[0]
        self.index = len(self.states) - 1

    @property
    def can_undo(self):
        return self.index > 0

    @property
    def can_redo(self):
        return self.index < len(self.states) - 1

    def undo(self):
        if not self.can_undo:
            return None
        self.index -= 1
        return self.states[self.index][1]

    def redo(self):
        if not self.can_redo:
            return None
        self.index += 1
        return self.states[self.index][1]

    def jump(self, index):
        if 0 <= index < len(self.states) and index != self.index:
            self.index = index
            return self.states[index][1]
        return None

    def labels(self):
        return [label for label, _ in self.states]

    def undo_label(self):
        return self.states[self.index][0] if self.can_undo else ""

    def redo_label(self):
        return self.states[self.index + 1][0] if self.can_redo else ""
