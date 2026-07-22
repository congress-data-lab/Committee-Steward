-- 0006_committee_event_notes.sql
-- Additive only: interpretive metadata for termination events. No changes to committee_event.

CREATE TABLE committee_event_note (
    event_id TEXT NOT NULL
        REFERENCES committee_event(event_id)
        ON DELETE CASCADE,

    note_type TEXT NOT NULL,

    interpretation_basis TEXT NOT NULL,

    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),

    created_by TEXT NOT NULL DEFAULT 'parser',

    PRIMARY KEY (event_id, note_type)
);

-- Controlled vocabulary enforcement (tight, deterministic)
ALTER TABLE committee_event_note
ADD CONSTRAINT committee_event_note_type_check
CHECK (
    note_type IN (
        'explicit_resignation',
        'implicit_leave',
        'full_exit_house',
        'multi_committee_statement',
        'header_body_split',
        'effective_date_explicit',
        'nonstandard_language'
    )
);

-- Index for lookups by event
CREATE INDEX committee_event_note_event_id_idx ON committee_event_note (event_id);
