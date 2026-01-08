from sqlalchemy import Column, Integer, String, Text, ForeignKey, DateTime, Boolean
from sqlalchemy.orm import relationship
from datetime import datetime
from app.database import Base

class Thread(Base):
    __tablename__ = "threads"

    id = Column(Integer, primary_key=True, index=True)
    thread_number = Column(Integer, unique=True, index=True)
    board_id = Column(String)
    subject = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    posts = relationship("Post", back_populates="thread")

class Post(Base):
    __tablename__ = "posts"

    id = Column(Integer, primary_key=True, index=True)
    post_number = Column(Integer, unique=True, index=True)
    thread_id = Column(Integer, ForeignKey("threads.id"))
    name = Column(String, nullable=True)
    metadata_raw = Column(String, nullable=True) # For Date/IP/ID
    posted_at = Column(DateTime, nullable=True)
    content = Column(Text, nullable=True)
    image_url = Column(String, nullable=True)
    local_image_path = Column(String, nullable=True)
    is_op = Column(Boolean, default=False)

    thread = relationship("Thread", back_populates="posts")
