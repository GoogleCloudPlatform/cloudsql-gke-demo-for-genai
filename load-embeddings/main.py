# Copyright 2024 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import asyncio
import os
import time

import asyncpg
import google.auth
from google.auth.transport.requests import Request as GRequest
from google.cloud import aiplatform
from langchain_google_vertexai import VertexAIEmbeddings
from langchain_text_splitters import RecursiveCharacterTextSplitter
import numpy as np
import pandas as pd
from pgvector.asyncpg import register_vector

DB_HOST = os.getenv("DB_HOST")
DB_USER = os.getenv("DB_USER")
DB_NAME = os.getenv("DB_NAME")
REGION = os.getenv("REGION")
PROJECT_ID = os.getenv("PROJECT_ID")
DATASET_FILE = "edinburgh_xsum_dataset.csv"


def load_dataset(location) -> pd.DataFrame:
    """Loads the dataset from the specified location"""
    df = pd.read_csv(location)
    df = df.rename(columns={"id": "article_id", "document": "article"})
    df = df.loc[:, ["article_id", "article", "summary"]]
    df = df.dropna()
    return df


async def load_into_db(conn: asyncpg.Connection, df: pd.DataFrame):
    """Loads data into a Postgres database table.

    This may take a few minutes to run."""
    await conn.execute("DROP TABLE IF EXISTS articles CASCADE")
    await conn.execute(
        """
        CREATE TABLE articles(
            article_id INTEGER PRIMARY KEY,
            article TEXT,
            summary TEXT
        );
        """
    )
    # Copy the dataframe to the `articles` table.
    tuples = list(df.itertuples(index=False))
    await conn.copy_records_to_table(
        "articles", records=tuples, columns=list(df), timeout=10
    )


def split_articles(df: pd.DataFrame):
    """Splits long articles into smaller chunks"""
    text_splitter = RecursiveCharacterTextSplitter(
        separators=[".", "\n"],
        chunk_size=500,
        chunk_overlap=0,
        length_function=len,
    )
    chunked = []
    for _, row in df.iterrows():
        article_id = row["article_id"]
        article = row["article"]
        splits = text_splitter.create_documents([article])
        for s in splits:
            r = {"article_id": article_id, "content": s.page_content}
            chunked.append(r)
    return chunked


def retry_with_backoff(func, *args, retry_delay=5, backoff_factor=2, **kwargs):
    """Helper function to retry failed API requests with exponential
    backoff."""
    max_attempts = 10
    retries = 0
    for _ in range(max_attempts):
        try:
            return func(*args, **kwargs)
        except Exception as e:
            print(f"error: {e}")
            retries += 1
            wait = retry_delay * (backoff_factor**retries)
            print(f"Retry after waiting for {wait} seconds...")
            time.sleep(wait)


def generate_vector_embeddings(df: pd.DataFrame):
    """Generate the vector embeddings for each chunk of text.

    Vertex AI text embedding model is used to generate vector embeddings,
    which outputs a 768-dimensional vector for each chunk of text.

    This may take a few minutes to run."""
    aiplatform.init(project=f"{PROJECT_ID}", location=f"{REGION}")
    embeddings_service = VertexAIEmbeddings(
        model_name="textembedding-gecko@003",
    )
    chunked = split_articles(df)

    batch_size = 5
    for i in range(0, len(chunked), batch_size):
        request = [x["content"] for x in chunked[i : i + batch_size]]
        response = retry_with_backoff(embeddings_service.embed_documents, request)
        # Store the retrieved vector embeddings for each chunk back.
        for x, e in zip(chunked[i : i + batch_size], response):
            x["embedding"] = e

    # Store the generated embeddings in a pandas dataframe.
    article_embeddings = pd.DataFrame(chunked)
    print(article_embeddings.head())

    return article_embeddings


async def store_embeddings_in_db(conn: asyncpg.Connection, article_embeddings):
    """Store the generated vector embeddings in a PostgreSQL table."""
    await conn.execute("DROP TABLE IF EXISTS article_embeddings")

    await conn.execute(
        """
        CREATE TABLE article_embeddings(
        article_id INTEGER NOT NULL REFERENCES articles(article_id),
        content TEXT,
        embedding vector(768)
        )
        """
    )

    # Store all the generated embeddings back into the database.
    for _, row in article_embeddings.iterrows():
        await conn.execute(
            """
            INSERT INTO article_embeddings
                (article_id, content, embedding)
            VALUES
                ($1, $2, $3)
            """,
            row["article_id"],
            row["content"],
            np.array(row["embedding"]),
        )


async def create_embeddings_index(conn: asyncpg.Connection):
    """Create indexes for faster similarity search in pgvector"""
    m = 24
    ef_construction = 100
    operator = "vector_cosine_ops"
    lists = 100

    # Create an HNSW index on the `article_embeddings` table.
    await conn.execute(
        f"""
        CREATE INDEX ON article_embeddings
          USING hnsw(embedding {operator})
          WITH (m = {m}, ef_construction = {ef_construction})
        """
    )

    # Create an IVFFLAT index on the `article_embeddings` table.
    await conn.execute(
        f"""
        CREATE INDEX ON article_embeddings
          USING ivfflat(embedding {operator})
          WITH (lists = {lists})
        """
    )


creds, _ = google.auth.default(
    scopes=["https://www.googleapis.com/auth/sqlservice.login"]
)


def get_password():
    if not creds.valid:
        request = GRequest()
        creds.refresh(request)

    return creds.token


async def main():
    print("Starting load-embeddings job...")
    df = load_dataset(DATASET_FILE)

    print(df.head(10))

    print("Creating connection pool...")
    async with asyncpg.create_pool(
        host=DB_HOST,
        user=DB_USER,
        password=get_password,
        database=DB_NAME,
        ssl="require",
    ) as pool:
        async with pool.acquire() as conn:
            print("Registering vector type...")
            await register_vector(conn)

            print("Loading dataset into db...")
            await load_into_db(conn, df)

            print("Generating embeddings...")
            embeddings = generate_vector_embeddings(df)

            print("Loading embeddings into db...")
            await store_embeddings_in_db(conn, embeddings)
            print("Creating embeddings index...")
            await create_embeddings_index(conn)

    print("Done")


if __name__ == "__main__":
    asyncio.run(main())
