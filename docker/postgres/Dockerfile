FROM postgres:17

# pgvector（埋め込み）と pg_partman（時系列パーティション管理）を apt で導入する。
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        postgresql-17-pgvector \
        postgresql-17-partman \
    && rm -rf /var/lib/apt/lists/*

# pg_partman は共有ライブラリのプリロードが必要。
RUN echo "shared_preload_libraries = 'pg_partman_bgw'" >> /usr/share/postgresql/postgresql.conf.sample
