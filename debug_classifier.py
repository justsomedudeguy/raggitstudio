import sys
sys.path.insert(0, 'backend/app')
from pathlib import Path
import tempfile
from customchat.api import create_app
from customchat.config import Settings
from customchat.database import Database

class FailingClassifierLemonade:
    def __init__(self):
        self.embed_model_ids = []
    async def list_models(self):
        return {'data': [{'id': 'embedder', 'labels': ['embeddings'], 'recipe': 'llamacpp'}]}
    async def classify(self, model_id, title, text, source_type):
        raise RuntimeError(f"Model {model_id} is not loaded")
    async def embed(self, model_id, texts):
        self.embed_model_ids.append(model_id)
        return [[1.0, 0.0]] * len(texts)

class FakeRedditDownloader:
    async def download(self, **kwargs):
        output_path = kwargs['output_path']
        kind = kwargs['kind']
        import json
        rows = []
        if kind == 'post':
            rows = [{'id':'p1','name':'t3_p1','subreddit':'theehive','author':'poster','created_utc':1710000000,'author_created_utc':1700000000,'author_total_karma':1234,'score':12,'title':'Alpha synthesis discussion','selftext':'alpha post evidence','permalink':'/r/theehive/comments/p1/a/'}]
        else:
            rows = [{'id':'c1','name':'t1_c1','subreddit':'theehive','author':'commenter','created_utc':1710000010,'score':5,'body':'alpha comment evidence','link_id':'t3_p1','parent_id':'t3_p1','permalink':'/r/theehive/comments/p1/_/c1/'}]
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text('\n'.join(json.dumps(row) for row in rows), encoding='utf-8')
        return {'path': str(output_path), 'count': len(rows), 'bytes': output_path.stat().st_size}

with tempfile.TemporaryDirectory() as tmp:
    tmp_path = Path(tmp)
    settings = Settings(
        app_root=tmp_path,
        lemonade_base_url='http://example.test/v1',
        chat_model_id='chat-model',
        embedding_model_id='embedder',
        reranker_model_id='reranker',
        classifier_model_id='chat-model',
        chunk_tokens=20,
        overlap_tokens=5,
    )
    db = Database(settings.database_path)
    db.initialize()
    app = create_app(
        settings=settings,
        database=db,
        lemonade=FailingClassifierLemonade(),
        reddit_downloader=FakeRedditDownloader(),
        run_reddit_imports_inline=True,
    )
    from fastapi.testclient import TestClient
    client = TestClient(app)
    started = client.post('/api/reddit-imports', json={
        'target_type': 'subreddit',
        'target_name': 'TheeHive',
        'start_date': '2024-01-01',
        'end_date': 'now',
        'include_posts': True,
        'include_comments': True,
    })
    job_id = started.json()['id']
    job = client.get(f'/api/reddit-imports/{job_id}').json()
    print('Status:', job.get('status'))
    print('Log:', job.get('log'))
    print('Stage:', job.get('current_stage'))
    print('Counts:', job.get('stage_counts'))
    # Check classifier_unavailable_items
    coverage = client.get('/api/archives/subreddits').json()
    if coverage.get('subreddits'):
        s = coverage['subreddits'][0]
        print('Classifier unavailable:', s.get('classifier_unavailable_items'))
