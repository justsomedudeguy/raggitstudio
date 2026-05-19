from __future__ import annotations

from pathlib import Path

from customchat.archive import ArchiveService, iter_archive_rows, normalize_reddit_item
from customchat.database import Database


def test_iter_archive_rows_streams_jsonl_rows(tmp_path: Path):
    path = tmp_path / "rows.jsonl"
    path.write_text('{"id":"a","body":"first"}\n{"id":"b","body":"second"}\n', encoding="utf-8")

    rows = list(iter_archive_rows(path))

    assert [row["id"] for row in rows] == ["a", "b"]


def test_normalize_reddit_item_extracts_post_selftext_and_comment_body():
    post = normalize_reddit_item(
        {
            "id": "abc",
            "name": "t3_abc",
            "subreddit": "theehive",
            "author": "poster",
            "created_utc": 1710000000,
            "score": 12,
            "title": "P2P update",
            "selftext": "post body",
            "url": "https://example.com/post",
            "permalink": "/r/theehive/comments/abc/p2p_update/",
        },
        fallback_kind="post",
        archive_file_id=4,
        line_number=9,
    )
    comment = normalize_reddit_item(
        {
            "id": "def",
            "name": "t1_def",
            "subreddit": "theehive",
            "author": "commenter",
            "created_utc": "1710000001",
            "score": 5,
            "body": "comment body",
            "link_id": "t3_abc",
            "parent_id": "t3_abc",
            "permalink": "/r/theehive/comments/abc/_/def/",
        },
        fallback_kind="comment",
        archive_file_id=4,
        line_number=10,
    )

    assert post.kind == "post"
    assert post.text == "P2P update\n\npost body"
    assert post.title == "P2P update"
    assert comment.kind == "comment"
    assert comment.text == "comment body"
    assert comment.link_id == "t3_abc"
    assert comment.parent_id == "t3_abc"


def test_archive_import_indexes_reddit_rows_and_exact_counts(tmp_path: Path):
    database = Database(tmp_path / "customchat.db")
    database.initialize()
    posts = tmp_path / "r_theehive_posts.jsonl"
    comments = tmp_path / "r_theehive_comments.jsonl"
    posts.write_text(
        "\n".join(
            [
                '{"id":"p1","subreddit":"theehive","author":"a","created_utc":1710000000,'
                '"score":1,"title":"P2P update","selftext":"P2P p2p","permalink":"/r/theehive/comments/p1/a/"}',
                '{"id":"p2","subreddit":"theehive","author":"b","created_utc":1710000002,'
                '"score":2,"title":"Other","selftext":"nothing here","permalink":"/r/theehive/comments/p2/b/"}',
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    comments.write_text(
        "\n".join(
            [
                '{"id":"c1","subreddit":"theehive","author":"c","created_utc":1710000003,'
                '"score":3,"body":"p2p P2P","link_id":"t3_p1","parent_id":"t3_p1",'
                '"permalink":"/r/theehive/comments/p1/_/c1/"}',
                '{"id":"c2","subreddit":"theehive","author":"d","created_utc":1710000004,'
                '"score":4,"body":"unrelated","link_id":"t3_p1","parent_id":"t1_c1",'
                '"permalink":"/r/theehive/comments/p1/_/c2/"}',
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    service = ArchiveService(database)

    post_result = service.import_file(posts)
    comment_result = service.import_file(comments)
    exact = service.count_occurrences(term="P2P", case_sensitive=True)
    folded = service.count_occurrences(term="p2p", case_sensitive=False)
    coverage = service.coverage()

    assert post_result.indexed_count == 2
    assert comment_result.indexed_count == 2
    assert exact.occurrences == 3
    assert exact.matched_items == 2
    assert exact.by_kind == {"comment": 1, "post": 2}
    assert folded.occurrences == 5
    assert folded.matched_items == 2
    assert coverage["items"] == 4
    assert coverage["posts"] == 2
    assert coverage["comments"] == 2
    assert coverage["embedded_items"] == 0


def test_archive_search_returns_raw_reddit_metadata(tmp_path: Path):
    database = Database(tmp_path / "customchat.db")
    database.initialize()
    path = tmp_path / "comments.jsonl"
    path.write_text(
        '{"id":"c1","subreddit":"theehive","author":"c","created_utc":1710000003,'
        '"score":3,"body":"needle in a haystack","link_id":"t3_p1","parent_id":"t3_p1",'
        '"permalink":"/r/theehive/comments/p1/_/c1/"}\n',
        encoding="utf-8",
    )
    service = ArchiveService(database)
    service.import_file(path)

    results = service.search(query="needle", limit=5)

    assert len(results) == 1
    assert results[0]["kind"] == "comment"
    assert results[0]["subreddit"] == "theehive"
    assert results[0]["body"] == "needle in a haystack"
    assert results[0]["raw"]["id"] == "c1"


def test_archive_import_keeps_empty_text_rows_as_raw_records(tmp_path: Path):
    database = Database(tmp_path / "customchat.db")
    database.initialize()
    path = tmp_path / "comments.jsonl"
    path.write_text(
        '{"id":"empty","subreddit":"theehive","author":"[deleted]","created_utc":1710000003,'
        '"score":0,"body":"","link_id":"t3_p1","parent_id":"t3_p1"}\n',
        encoding="utf-8",
    )
    service = ArchiveService(database)

    result = service.import_file(path, kind="comment")
    rows = service.search(query="", limit=5)

    assert result.status == "completed"
    assert result.indexed_count == 1
    assert result.failed_count == 0
    assert rows[0]["reddit_id"] == "empty"
    assert rows[0]["text"] == ""
