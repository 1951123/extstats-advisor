from __future__ import annotations

import os

import pytest


@pytest.fixture(scope="session")
def postgres_capture_dsn() -> str:
    capture_dsn = os.environ.get("EXTSTATS_ADVISOR_TEST_POSTGRES_DSN")
    admin_dsn = os.environ.get("EXTSTATS_ADVISOR_TEST_POSTGRES_ADMIN_DSN")
    if not capture_dsn or not admin_dsn:
        pytest.skip(
            "set EXTSTATS_ADVISOR_TEST_POSTGRES_DSN and "
            "EXTSTATS_ADVISOR_TEST_POSTGRES_ADMIN_DSN for stock PostgreSQL integration"
        )
    try:
        import psycopg
    except ImportError:
        pytest.skip("install extstats-advisor[postgres] for PostgreSQL integration")

    with psycopg.connect(admin_dsn, autocommit=True) as connection:
        connection.execute('DROP SCHEMA IF EXISTS "Reporting.Schema" CASCADE')
        connection.execute(
            """
            DO $do$
            BEGIN
                IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'extstats_capture') THEN
                    CREATE ROLE extstats_capture LOGIN PASSWORD 'capture-secret'
                        NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT;
                ELSE
                    ALTER ROLE extstats_capture LOGIN PASSWORD 'capture-secret'
                        NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT;
                END IF;
            END
            $do$
            """
        )
        connection.execute('CREATE SCHEMA "Reporting.Schema" AUTHORIZATION CURRENT_USER')
        connection.execute(
            """
            CREATE TABLE "Reporting.Schema"."Order Facts" (
                "Customer ID" bigint NOT NULL,
                "Small Value" smallint,
                "Integer Value" integer,
                "Real Value" real,
                "Double Value" double precision,
                "Amount" numeric(10,2),
                "Description" text,
                "Code" varchar(20),
                "Flag" boolean,
                "Payload" bytea,
                "Order Date" date,
                "Created At" timestamp without time zone,
                "Observed At" timestamp with time zone,
                "Order UUID" uuid,
                "城市" character(8)
            )
            """
        )
        connection.execute(
            """
            INSERT INTO "Reporting.Schema"."Order Facts"
            SELECT g, g::smallint, g, g::real, g::double precision, (g * 1.25)::numeric(10,2),
                   'row-' || g, 'C' || g, (g % 2 = 0), decode(md5(g::text), 'hex'),
                   DATE '2025-01-01' + g, TIMESTAMP '2025-01-01 00:00:00' + g * INTERVAL '1 hour',
                   TIMESTAMPTZ '2025-01-01 00:00:00+05:00' + g * INTERVAL '1 hour',
                   md5(g::text)::uuid, rpad('city' || g, 8, ' ')
            FROM generate_series(1, 100) AS g
            """
        )
        connection.execute('ANALYZE "Reporting.Schema"."Order Facts"')
        connection.execute(
            """
            CREATE TABLE "Reporting.Schema"."Unsupported JSON" ("Payload" jsonb)
            """
        )
        connection.execute(
            """
            CREATE TABLE "Reporting.Schema"."RLS Table" ("ID" integer, "Secret" text)
            """
        )
        connection.execute('INSERT INTO "Reporting.Schema"."RLS Table" VALUES (1, \'hidden\')')
        connection.execute('ALTER TABLE "Reporting.Schema"."RLS Table" ENABLE ROW LEVEL SECURITY')
        connection.execute('GRANT USAGE ON SCHEMA "Reporting.Schema" TO extstats_capture')
        connection.execute(
            'GRANT SELECT ON ALL TABLES IN SCHEMA "Reporting.Schema" TO extstats_capture'
        )
    yield capture_dsn
    with psycopg.connect(admin_dsn, autocommit=True) as connection:
        connection.execute('DROP SCHEMA IF EXISTS "Reporting.Schema" CASCADE')
