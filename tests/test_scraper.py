import os
import pytest
from app.scraper import parse_and_save
from app import models
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

# Mock the database
@pytest.fixture(scope="module")
def test_db():
    engine = create_engine('sqlite:///:memory:')
    models.Base.metadata.create_all(engine)
    TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    yield TestingSessionLocal
    models.Base.metadata.drop_all(engine)

@pytest.fixture
def mock_session(test_db, mocker):
    session = test_db()
    # Mock SessionLocal in scraper to return our test session
    mocker.patch('app.scraper.SessionLocal', return_value=session)
    yield session
    session.close()

def test_metadata_extraction(mock_session, mocker):
    # Load the page dump
    with open('page_dump.html', 'r', encoding='shift_jis', errors='ignore') as f:
        html_content = f.read()

    # Run the parser
    # We might need to mock requests if parse_and_save does networking, 
    # but based on the code it seems to only parse and save to DB.
    # However, download_image might be called. Let's mock download_image to avoid network calls.
    
    mocker.patch('app.scraper.download_image', return_value='/tmp/mock_image.jpg')
    parse_and_save(html_content)

    # Check the database
    # We expect some posts. 
    # Let's check a specific post we saw in the HTML: No.2682763
    # <span class="cnw">25/12/14(日)18:22:44 IP:49.98.*(spmode.ne.jp)</span>
    
    post = mock_session.query(models.Post).filter_by(post_number=2682763).first()
    assert post is not None
    assert post.metadata_raw == "25/12/14(日)18:22:44 IP:49.98.*(spmode.ne.jp)"
    # 2025-12-14 18:22:44
    assert post.posted_at is not None
    assert post.posted_at.year == 2025
    assert post.posted_at.month == 12
    assert post.posted_at.day == 14
    assert post.posted_at.hour == 18
    assert post.posted_at.minute == 22
    assert post.posted_at.second == 44
