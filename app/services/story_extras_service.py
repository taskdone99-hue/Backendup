"""
Story mentions, polls, and questions — created alongside a story (see
story_routes.create_story) and voted/answered on afterward.
"""

from sqlalchemy.orm import Session

from app import models


def attach_mentions(db: Session, story: models.Story, mentioned_user_ids: list[int]) -> None:
    """De-duplicates and skips self-mentions; doesn't validate the ids
    exist (bad ids are silently dropped by the FK... no — SQLite/MySQL
    would raise on a truly invalid id, so callers should pass already-
    validated ids). Caller commits."""
    seen: set[int] = set()
    for uid in mentioned_user_ids:
        if uid == story.user_id or uid in seen:
            continue
        seen.add(uid)
        db.add(models.StoryMention(story_id=story.id, user_id=uid))


def attach_poll(db: Session, story: models.Story, question: str, option_texts: list[str]) -> models.StoryPoll:
    poll = models.StoryPoll(story_id=story.id, question=question)
    db.add(poll)
    db.flush()
    for position, text in enumerate(option_texts):
        db.add(models.StoryPollOption(poll_id=poll.id, text=text, position=position))
    return poll


def attach_question(db: Session, story: models.Story, prompt: str) -> models.StoryQuestion:
    question = models.StoryQuestion(story_id=story.id, prompt=prompt)
    db.add(question)
    return question


def to_poll_out(poll: models.StoryPoll, viewer_id: int | None):
    from app import schemas  # local import: avoids a schemas<->models import cycle

    votes_by_option: dict[int, int] = {}
    my_vote_option_id = None
    for option in poll.options:
        votes_by_option[option.id] = len(option.votes)
        if viewer_id is not None:
            for vote in option.votes:
                if vote.user_id == viewer_id:
                    my_vote_option_id = option.id

    return schemas.StoryPollOut(
        id=poll.id,
        question=poll.question,
        options=[
            schemas.StoryPollOptionOut(id=o.id, text=o.text, votes_count=votes_by_option[o.id])
            for o in poll.options
        ],
        total_votes=sum(votes_by_option.values()),
        my_vote_option_id=my_vote_option_id,
    )


def to_question_out(question: models.StoryQuestion):
    from app import schemas

    return schemas.StoryQuestionOut(
        id=question.id, prompt=question.prompt, responses_count=len(question.responses)
    )
