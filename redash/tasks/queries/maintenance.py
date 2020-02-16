import logging
import time

from rq.timeouts import JobTimeoutException
from redash import models, redis_connection, settings, statsd_client
from redash.models.parameterized_query import (
    InvalidParameterError,
    QueryDetachedFromDataSourceError,
)
from redash.tasks.failure_report import track_failure
from redash.utils import json_dumps
from redash.worker import job, get_job_logger

from .execution import enqueue_query

logger = get_job_logger(__name__)


def empty_schedules():
    logger.info("Deleting schedules of past scheduled queries...")

    queries = models.Query.past_scheduled_queries()
    for query in queries:
        query.schedule = None
    models.db.session.commit()

    logger.info("Deleted %d schedules.", len(queries))


def refresh_queries():
    logger.info("Refreshing queries...")

    outdated_queries_count = 0
    query_ids = []

    with statsd_client.timer("manager.outdated_queries_lookup"):
        for query in models.Query.outdated_queries():
            if settings.FEATURE_DISABLE_REFRESH_QUERIES:
                logger.info("Disabled refresh queries.")
            elif query.org.is_disabled:
                logger.debug(
                    "Skipping refresh of %s because org is disabled.", query.id
                )
            elif query.data_source is None:
                logger.debug(
                    "Skipping refresh of %s because the datasource is none.", query.id
                )
            elif query.data_source.paused:
                logger.debug(
                    "Skipping refresh of %s because datasource - %s is paused (%s).",
                    query.id,
                    query.data_source.name,
                    query.data_source.pause_reason,
                )
            else:
                query_text = query.query_text

                parameters = {p["name"]: p.get("value") for p in query.parameters}
                if any(parameters):
                    try:
                        query_text = query.parameterized.apply(parameters).query
                    except InvalidParameterError as e:
                        error = u"Skipping refresh of {} because of invalid parameters: {}".format(
                            query.id, str(e)
                        )
                        track_failure(query, error)
                        continue
                    except QueryDetachedFromDataSourceError as e:
                        error = (
                            "Skipping refresh of {} because a related dropdown "
                            "query ({}) is unattached to any datasource."
                        ).format(query.id, e.query_id)
                        track_failure(query, error)
                        continue

                enqueue_query(
                    query_text,
                    query.data_source,
                    query.user_id,
                    scheduled_query=query,
                    metadata={"Query ID": query.id, "Username": "Scheduled"},
                )

                query_ids.append(query.id)
                outdated_queries_count += 1

    statsd_client.gauge("manager.outdated_queries", outdated_queries_count)

    logger.info(
        "Done refreshing queries. Found %d outdated queries: %s"
        % (outdated_queries_count, query_ids)
    )

    status = redis_connection.hgetall("redash:status")
    now = time.time()

    redis_connection.hmset(
        "redash:status",
        {
            "outdated_queries_count": outdated_queries_count,
            "last_refresh_at": now,
            "query_ids": json_dumps(query_ids),
        },
    )

    statsd_client.gauge(
        "manager.seconds_since_refresh", now - float(status.get("last_refresh_at", now))
    )


def cleanup_query_results():
    """
    Job to cleanup unused query results -- such that no query links to them anymore, and older than
    settings.QUERY_RESULTS_MAX_AGE (a week by default, so it's less likely to be open in someone's browser and be used).

    Each time the job deletes only settings.QUERY_RESULTS_CLEANUP_COUNT (100 by default) query results so it won't choke
    the database in case of many such results.
    """

    logger.info(
        "Running query results clean up (removing maximum of %d unused results, that are %d days old or more)",
        settings.QUERY_RESULTS_CLEANUP_COUNT,
        settings.QUERY_RESULTS_CLEANUP_MAX_AGE,
    )

    unused_query_results = models.QueryResult.unused(
        settings.QUERY_RESULTS_CLEANUP_MAX_AGE
    )
    deleted_count = models.QueryResult.query.filter(
        models.QueryResult.id.in_(
            unused_query_results.limit(settings.QUERY_RESULTS_CLEANUP_COUNT).subquery()
        )
    ).delete(synchronize_session=False)
    models.db.session.commit()
    logger.info("Deleted %d unused query results.", deleted_count)


@job("schemas")
def refresh_schema(data_source_id):
    ds = models.DataSource.get_by_id(data_source_id)
    logger.info(u"task=refresh_schema state=start ds_id=%s", ds.id)
    start_time = time.time()
    try:
        ds.get_schema(refresh=True)
        logger.info(
            u"task=refresh_schema state=finished ds_id=%s runtime=%.2f",
            ds.id,
            time.time() - start_time,
        )
        statsd_client.incr("refresh_schema.success")
    except JobTimeoutException:
        logger.info(
            u"task=refresh_schema state=timeout ds_id=%s runtime=%.2f",
            ds.id,
            time.time() - start_time,
        )
        statsd_client.incr("refresh_schema.timeout")
    except Exception:
        logger.warning(
            u"Failed refreshing schema for the data source: %s", ds.name, exc_info=1
        )
        statsd_client.incr("refresh_schema.error")
        logger.info(
            u"task=refresh_schema state=failed ds_id=%s runtime=%.2f",
            ds.id,
            time.time() - start_time,
        )


def refresh_schemas():
    """
    Refreshes the data sources schemas.
    """
    blacklist = [
        int(ds_id)
        for ds_id in redis_connection.smembers("data_sources:schema:blacklist")
        if ds_id
    ]
    global_start_time = time.time()

    logger.info(u"task=refresh_schemas state=start")

    for ds in models.DataSource.query:
        if ds.paused:
            logger.info(
                u"task=refresh_schema state=skip ds_id=%s reason=paused(%s)",
                ds.id,
                ds.pause_reason,
            )
        elif ds.id in blacklist:
            logger.info(
                u"task=refresh_schema state=skip ds_id=%s reason=blacklist", ds.id
            )
        elif ds.org.is_disabled:
            logger.info(
                u"task=refresh_schema state=skip ds_id=%s reason=org_disabled", ds.id
            )
        else:
            refresh_schema.delay(ds.id)

    logger.info(
        u"task=refresh_schemas state=finish total_runtime=%.2f",
        time.time() - global_start_time,
    )
