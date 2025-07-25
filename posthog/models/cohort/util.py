import uuid
from datetime import datetime, timedelta
from typing import Any, Optional, Union, cast

import structlog
from dateutil import parser
from django.conf import settings
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from posthog.hogql.resolver_utils import extract_select_queries
from posthog.queries.util import PersonPropertiesMode
from posthog.clickhouse.client.connection import Workload, ClickHouseUser
from posthog.clickhouse.query_tagging import tag_queries, tags_context, Feature
from posthog.clickhouse.client import sync_execute
from posthog.constants import PropertyOperatorType
from posthog.hogql import ast
from posthog.hogql.constants import LimitContext, HogQLGlobalSettings
from posthog.hogql.hogql import HogQLContext
from posthog.hogql.modifiers import create_default_modifiers_for_team
from posthog.hogql.printer import print_ast
from posthog.models import Action, Filter, Team
from posthog.models.action.util import format_action_filter
from posthog.models.cohort.cohort import Cohort, CohortOrEmpty
from posthog.models.cohort.sql import (
    CALCULATE_COHORT_PEOPLE_SQL,
    GET_COHORT_SIZE_SQL,
    GET_COHORTS_BY_PERSON_UUID,
    GET_PERSON_ID_BY_PRECALCULATED_COHORT_ID,
    GET_STATIC_COHORT_SIZE_SQL,
    GET_STATIC_COHORTPEOPLE_BY_PERSON_UUID,
    RECALCULATE_COHORT_BY_ID,
)
from posthog.models.person.sql import (
    INSERT_PERSON_STATIC_COHORT,
    PERSON_STATIC_COHORT_TABLE,
)
from posthog.models.property import Property, PropertyGroup
from posthog.queries.person_distinct_id_query import get_team_distinct_ids_query

# temporary marker to denote when cohortpeople table started being populated
TEMP_PRECALCULATED_MARKER = parser.parse("2021-06-07T15:00:00+00:00")

logger = structlog.get_logger(__name__)


def format_person_query(cohort: Cohort, index: int, hogql_context: HogQLContext) -> tuple[str, dict[str, Any]]:
    if cohort.is_static:
        return format_static_cohort_query(cohort, index, prepend="")

    if not cohort.properties.values:
        # No person can match an empty cohort
        return "SELECT generateUUIDv4() as id WHERE 0 = 19", {}

    from posthog.queries.cohort_query import CohortQuery

    query_builder = CohortQuery(
        Filter(
            data={"properties": cohort.properties},
            team=cohort.team,
            hogql_context=hogql_context,
        ),
        cohort.team,
        cohort_pk=cohort.pk,
        persons_on_events_mode=cohort.team.person_on_events_mode,
    )

    query, params = query_builder.get_query()

    return query, params


def print_cohort_hogql_query(cohort: Cohort, hogql_context: HogQLContext, *, team: Team) -> str:
    from posthog.hogql_queries.query_runner import get_query_runner

    if not cohort.query:
        raise ValueError("Cohort has no query")

    query = get_query_runner(
        cast(dict, cohort.query), team=team, limit_context=LimitContext.COHORT_CALCULATION
    ).to_query()

    for select_query in extract_select_queries(query):
        columns: dict[str, ast.Expr] = {}
        for expr in select_query.select:
            if isinstance(expr, ast.Alias):
                columns[expr.alias] = expr.expr
            elif isinstance(expr, ast.Field):
                columns[str(expr.chain[-1])] = expr
        column: ast.Expr | None = columns.get("person_id") or columns.get("actor_id") or columns.get("id")
        if isinstance(column, ast.Alias):
            select_query.select = [ast.Alias(expr=column.expr, alias="actor_id")]
        elif isinstance(column, ast.Field):
            select_query.select = [ast.Alias(expr=column, alias="actor_id")]
        else:
            # Support the most common use cases
            table = select_query.select_from.table if select_query.select_from else None
            if isinstance(table, ast.Field) and table.chain[-1] == "events":
                select_query.select = [ast.Alias(expr=ast.Field(chain=["person", "id"]), alias="actor_id")]
            elif isinstance(table, ast.Field) and table.chain[-1] == "persons":
                select_query.select = [ast.Alias(expr=ast.Field(chain=["id"]), alias="actor_id")]
            else:
                raise ValueError("Could not find a person_id, actor_id, or id column in the query")

    hogql_context.enable_select_queries = True
    hogql_context.limit_top_select = False
    create_default_modifiers_for_team(team, hogql_context.modifiers)

    # Apply HogQL global settings to ensure consistency with regular queries
    settings = HogQLGlobalSettings()
    return print_ast(query, context=hogql_context, dialect="clickhouse", settings=settings)


def format_static_cohort_query(cohort: Cohort, index: int, prepend: str) -> tuple[str, dict[str, Any]]:
    cohort_id = cohort.pk
    return (
        f"SELECT person_id as id FROM {PERSON_STATIC_COHORT_TABLE} WHERE cohort_id = %({prepend}_cohort_id_{index})s AND team_id = %(team_id)s",
        {f"{prepend}_cohort_id_{index}": cohort_id},
    )


def format_precalculated_cohort_query(cohort: Cohort, index: int, prepend: str = "") -> tuple[str, dict[str, Any]]:
    filter_query = GET_PERSON_ID_BY_PRECALCULATED_COHORT_ID.format(index=index, prepend=prepend)
    return (
        filter_query,
        {
            f"{prepend}_cohort_id_{index}": cohort.pk,
            f"{prepend}_version_{index}": cohort.version,
        },
    )


def get_count_operator(count_operator: Optional[str]) -> str:
    if count_operator == "gte":
        return ">="
    elif count_operator == "lte":
        return "<="
    elif count_operator == "gt":
        return ">"
    elif count_operator == "lt":
        return "<"
    elif count_operator == "eq" or count_operator == "exact" or count_operator is None:
        return "="
    else:
        raise ValidationError("count_operator must be gte, lte, eq, or None")


def get_count_operator_ast(count_operator: Optional[str]) -> ast.CompareOperationOp:
    if count_operator == "gte":
        return ast.CompareOperationOp.GtEq
    elif count_operator == "lte":
        return ast.CompareOperationOp.LtEq
    elif count_operator == "gt":
        return ast.CompareOperationOp.Gt
    elif count_operator == "lt":
        return ast.CompareOperationOp.Lt
    elif count_operator == "eq" or count_operator == "exact" or count_operator is None:
        return ast.CompareOperationOp.Eq
    else:
        raise ValidationError("count_operator must be gte, lte, eq, or None")


def get_entity_query(
    event_id: Optional[str],
    action_id: Optional[int],
    team_id: int,
    group_idx: Union[int, str],
    hogql_context: HogQLContext,
    person_properties_mode: Optional[PersonPropertiesMode] = None,
) -> tuple[str, dict[str, str]]:
    if event_id:
        return f"event = %({f'event_{group_idx}'})s", {f"event_{group_idx}": event_id}
    elif action_id:
        action = Action.objects.get(pk=action_id)
        action_filter_query, action_params = format_action_filter(
            team_id=team_id,
            action=action,
            prepend="_{}_action".format(group_idx),
            hogql_context=hogql_context,
            person_properties_mode=(
                person_properties_mode if person_properties_mode else PersonPropertiesMode.USING_SUBQUERY
            ),
        )
        return action_filter_query, action_params
    else:
        raise ValidationError("Cohort query requires action_id or event_id")


def get_date_query(
    days: Optional[str], start_time: Optional[str], end_time: Optional[str]
) -> tuple[str, dict[str, str]]:
    date_query: str = ""
    date_params: dict[str, str] = {}
    if days:
        date_query, date_params = parse_entity_timestamps_in_days(int(days))
    elif start_time or end_time:
        date_query, date_params = parse_cohort_timestamps(start_time, end_time)

    return date_query, date_params


def parse_entity_timestamps_in_days(days: int) -> tuple[str, dict[str, str]]:
    curr_time = timezone.now()
    start_time = curr_time - timedelta(days=days)

    return (
        "AND timestamp >= %(date_from)s AND timestamp <= %(date_to)s",
        {
            "date_from": start_time.strftime("%Y-%m-%d %H:%M:%S"),
            "date_to": curr_time.strftime("%Y-%m-%d %H:%M:%S"),
        },
    )


def parse_cohort_timestamps(start_time: Optional[str], end_time: Optional[str]) -> tuple[str, dict[str, str]]:
    clause = "AND "
    params: dict[str, str] = {}

    if start_time:
        clause += "timestamp >= %(date_from)s"

        params = {"date_from": datetime.strptime(start_time, "%Y-%m-%dT%H:%M:%S").strftime("%Y-%m-%d %H:%M:%S")}
    if end_time:
        clause += "timestamp <= %(date_to)s"
        params = {
            **params,
            "date_to": datetime.strptime(end_time, "%Y-%m-%dT%H:%M:%S").strftime("%Y-%m-%d %H:%M:%S"),
        }

    return clause, params


def is_precalculated_query(cohort: Cohort) -> bool:
    if (
        cohort.last_calculation
        and cohort.last_calculation > TEMP_PRECALCULATED_MARKER
        and settings.USE_PRECALCULATED_CH_COHORT_PEOPLE
        and not cohort.is_static  # static cohorts are handled within the regular cohort filter query path
    ):
        return True
    else:
        return False


def format_filter_query(
    cohort: Cohort,
    index: int,
    hogql_context: HogQLContext,
    id_column: str = "distinct_id",
    custom_match_field="person_id",
) -> tuple[str, dict[str, Any]]:
    person_query, params = format_cohort_subquery(cohort, index, hogql_context, custom_match_field=custom_match_field)

    person_id_query = CALCULATE_COHORT_PEOPLE_SQL.format(
        query=person_query,
        id_column=id_column,
        GET_TEAM_PERSON_DISTINCT_IDS=get_team_distinct_ids_query(cohort.team_id),
    )
    return person_id_query, params


def format_cohort_subquery(
    cohort: Cohort, index: int, hogql_context: HogQLContext, custom_match_field="person_id"
) -> tuple[str, dict[str, Any]]:
    is_precalculated = is_precalculated_query(cohort)
    if is_precalculated:
        query, params = format_precalculated_cohort_query(cohort, index)
    else:
        query, params = format_person_query(cohort, index, hogql_context)

    person_query = f"{custom_match_field} IN ({query})"
    return person_query, params


def insert_static_cohort(person_uuids: list[Optional[uuid.UUID]], cohort_id: int, *, team_id: int):
    tag_queries(cohort_id=cohort_id, team_id=team_id, name="insert_static_cohort", feature=Feature.COHORT)
    persons = [
        {
            "id": str(uuid.uuid4()),
            "person_id": str(person_uuid),
            "cohort_id": cohort_id,
            "team_id": team_id,
            "_timestamp": datetime.now(),
        }
        for person_uuid in person_uuids
    ]
    sync_execute(INSERT_PERSON_STATIC_COHORT, persons)


def get_static_cohort_size(*, cohort_id: int, team_id: int) -> Optional[int]:
    tag_queries(cohort_id=cohort_id, team_id=team_id, name="get_static_cohort_size", feature=Feature.COHORT)
    count_result = sync_execute(
        GET_STATIC_COHORT_SIZE_SQL,
        {
            "cohort_id": cohort_id,
            "team_id": team_id,
        },
    )

    if count_result and len(count_result) and len(count_result[0]):
        return count_result[0][0]
    else:
        return None


def recalculate_cohortpeople(
    cohort: Cohort, pending_version: int, *, initiating_user_id: Optional[int]
) -> Optional[int]:
    """
    Recalculate cohort people for all environments of the project.
    NOTE: Currently, this only returns the count for the team where the cohort was created. Instead, it should return for all teams.
    """
    relevant_teams = Team.objects.order_by("id").filter(project_id=cohort.team.project_id)
    count_by_team_id: dict[int, int] = {}
    tag_queries(cohort_id=cohort.id)
    if initiating_user_id:
        tag_queries(user_id=initiating_user_id)
    for team in relevant_teams:
        tag_queries(team_id=team.id)
        _recalculate_cohortpeople_for_team_hogql(cohort, pending_version, team, initiating_user_id=initiating_user_id)
        count = get_cohort_size(cohort, override_version=pending_version, team_id=team.id)
        count_by_team_id[team.id] = count or 0

    return count_by_team_id[cohort.team_id]


def _recalculate_cohortpeople_for_team_hogql(
    cohort: Cohort, pending_version: int, team: Team, *, initiating_user_id: Optional[int]
) -> int:
    tag_queries(name="recalculate_cohortpeople_for_team_hogql")
    cohort_params: dict[str, Any]
    # No need to do anything here, as we're only testing hogql
    if cohort.is_static:
        cohort_query, cohort_params = format_static_cohort_query(cohort, 0, prepend="")
    elif not cohort.properties.values:
        # Can't match anything, don't insert anything
        cohort_query = "SELECT generateUUIDv4() as id WHERE 0 = 19"
        cohort_params = {}
    else:
        from posthog.hogql_queries.hogql_cohort_query import HogQLCohortQuery

        cohort_query, hogql_context = (
            HogQLCohortQuery(cohort=cohort, team=team).get_query_executor().generate_clickhouse_sql()
        )
        cohort_params = hogql_context.values

        # Hacky: Clickhouse doesn't like there being a top level "SETTINGS" clause in a SelectSet statement when that SelectSet
        # statement is used in a subquery. We remove it here.
        cohort_query = cohort_query[: cohort_query.rfind("SETTINGS")]

    recalculate_cohortpeople_sql = RECALCULATE_COHORT_BY_ID.format(cohort_filter=cohort_query)

    tag_queries(kind="cohort_calculation", query_type="CohortsQueryHogQL", feature=Feature.COHORT)
    hogql_global_settings = HogQLGlobalSettings()

    return sync_execute(
        recalculate_cohortpeople_sql,
        {
            **cohort_params,
            "cohort_id": cohort.pk,
            "team_id": team.id,
            "new_version": pending_version,
        },
        settings={
            "max_execution_time": 600,
            "send_timeout": 600,
            "receive_timeout": 600,
            "optimize_on_insert": 0,
            "max_ast_elements": hogql_global_settings.max_ast_elements,
            "max_expanded_ast_elements": hogql_global_settings.max_expanded_ast_elements,
            "max_bytes_ratio_before_external_group_by": 0.5,
            "max_bytes_ratio_before_external_sort": 0.5,
        },
        workload=Workload.OFFLINE,
        ch_user=ClickHouseUser.COHORTS,
    )


def get_cohort_size(cohort: Cohort, override_version: Optional[int] = None, *, team_id: int) -> Optional[int]:
    tag_queries(name="get_cohort_size", feature=Feature.COHORT)
    count_result = sync_execute(
        GET_COHORT_SIZE_SQL,
        {
            "cohort_id": cohort.pk,
            "version": override_version if override_version is not None else cohort.version,
            "team_id": team_id,
        },
        workload=Workload.OFFLINE,
        ch_user=ClickHouseUser.COHORTS,
    )

    if count_result and len(count_result) and len(count_result[0]):
        return count_result[0][0]
    else:
        return None


def simplified_cohort_filter_properties(cohort: Cohort, team: Team, is_negated=False) -> PropertyGroup:
    """
    'Simplifies' cohort property filters, removing team-specific context from properties.
    """
    if cohort.is_static:
        return PropertyGroup(
            type=PropertyOperatorType.AND,
            values=[Property(type="static-cohort", key="id", value=cohort.pk, negation=is_negated)],
        )

    # Cohort has been precalculated
    if is_precalculated_query(cohort):
        return PropertyGroup(
            type=PropertyOperatorType.AND,
            values=[
                Property(
                    type="precalculated-cohort",
                    key="id",
                    value=cohort.pk,
                    negation=is_negated,
                )
            ],
        )

    # Cohort can have multiple match groups.
    # Each group is either
    # 1. "user has done X in time range Y at least N times" or
    # 2. "user has properties XYZ", including belonging to another cohort
    #
    # Users who match _any_ of the groups are considered to match the cohort.

    for property in cohort.properties.flat:
        if property.type == "behavioral":
            # TODO: Support behavioral property type in other insights
            return PropertyGroup(
                type=PropertyOperatorType.AND,
                values=[Property(type="cohort", key="id", value=cohort.pk, negation=is_negated)],
            )

        elif property.type == "cohort":
            # If entire cohort is negated, just return the negated cohort.
            if is_negated:
                return PropertyGroup(
                    type=PropertyOperatorType.AND,
                    values=[
                        Property(
                            type="cohort",
                            key="id",
                            value=cohort.pk,
                            negation=is_negated,
                        )
                    ],
                )
            # :TRICKY: We need to ensure we don't have infinite loops in here
            # guaranteed during cohort creation
            return Filter(data={"properties": cohort.properties.to_dict()}, team=team).property_groups

    # We have person properties only
    # TODO: Handle negating a complete property group
    if is_negated:
        return PropertyGroup(
            type=PropertyOperatorType.AND,
            values=[Property(type="cohort", key="id", value=cohort.pk, negation=is_negated)],
        )
    else:
        return cohort.properties


def _get_cohort_ids_by_person_uuid(uuid: str, team_id: int) -> list[int]:
    tag_queries(name="get_cohort_ids_by_person_uuid", feature=Feature.COHORT)
    res = sync_execute(GET_COHORTS_BY_PERSON_UUID, {"person_id": uuid, "team_id": team_id})
    return [row[0] for row in res]


def _get_static_cohort_ids_by_person_uuid(uuid: str, team_id: int) -> list[int]:
    tag_queries(name="get_static_cohort_ids_by_person_uuid", feature=Feature.COHORT)
    res = sync_execute(GET_STATIC_COHORTPEOPLE_BY_PERSON_UUID, {"person_id": uuid, "team_id": team_id})
    return [row[0] for row in res]


def get_all_cohort_ids_by_person_uuid(uuid: str, team_id: int) -> list[int]:
    with tags_context(team_id=team_id):
        cohort_ids = _get_cohort_ids_by_person_uuid(uuid, team_id)
        static_cohort_ids = _get_static_cohort_ids_by_person_uuid(uuid, team_id)
    return [*cohort_ids, *static_cohort_ids]


def get_dependent_cohorts(
    cohort: Cohort,
    using_database: str = "default",
    seen_cohorts_cache: Optional[dict[int, CohortOrEmpty]] = None,
) -> list[Cohort]:
    if seen_cohorts_cache is None:
        seen_cohorts_cache = {}

    cohorts = []
    seen_cohort_ids = set()
    seen_cohort_ids.add(cohort.id)

    queue = []
    for prop in cohort.properties.flat:
        if prop.type == "cohort" and not isinstance(prop.value, list):
            try:
                queue.append(int(prop.value))
            except (ValueError, TypeError):
                continue

    while queue:
        cohort_id = queue.pop()
        try:
            if cohort_id in seen_cohorts_cache:
                current_cohort = seen_cohorts_cache[cohort_id]
                if not current_cohort:
                    continue
            else:
                current_cohort = Cohort.objects.db_manager(using_database).get(
                    pk=cohort_id, team__project_id=cohort.team.project_id, deleted=False
                )
                seen_cohorts_cache[cohort_id] = current_cohort
            if current_cohort.id not in seen_cohort_ids:
                cohorts.append(current_cohort)
                seen_cohort_ids.add(current_cohort.id)

                for prop in current_cohort.properties.flat:
                    if prop.type == "cohort" and not isinstance(prop.value, list):
                        try:
                            queue.append(int(prop.value))
                        except (ValueError, TypeError):
                            continue

        except Cohort.DoesNotExist:
            seen_cohorts_cache[cohort_id] = ""
            continue

    return cohorts


def sort_cohorts_topologically(cohort_ids: set[int], seen_cohorts_cache: dict[int, CohortOrEmpty]) -> list[int]:
    """
    Sorts the given cohorts in an order where cohorts with no dependencies are placed first,
    followed by cohorts that depend on the preceding ones. It ensures that each cohort in the sorted list
    only depends on cohorts that appear earlier in the list.
    """

    if not cohort_ids:
        return []

    dependency_graph: dict[int, list[int]] = {}
    seen = set()

    # build graph (adjacency list)
    def traverse(cohort):
        # add parent
        dependency_graph[cohort.id] = []
        for prop in cohort.properties.flat:
            if prop.type == "cohort" and not isinstance(prop.value, list):
                # add child
                dependency_graph[cohort.id].append(int(prop.value))

                neighbor_cohort = seen_cohorts_cache.get(int(prop.value))
                if not neighbor_cohort:
                    continue

                if cohort.id not in seen:
                    seen.add(cohort.id)
                    traverse(neighbor_cohort)

    for cohort_id in cohort_ids:
        cohort = seen_cohorts_cache.get(int(cohort_id))
        if not cohort:
            continue
        traverse(cohort)

    # post-order DFS (children first, then the parent)
    def dfs(node, seen, sorted_arr):
        neighbors = dependency_graph.get(node, [])
        for neighbor in neighbors:
            if neighbor not in seen:
                dfs(neighbor, seen, sorted_arr)
        if seen_cohorts_cache.get(node):
            sorted_arr.append(int(node))
        seen.add(node)

    sorted_cohort_ids: list[int] = []
    seen = set()
    for cohort_id in cohort_ids:
        if cohort_id not in seen:
            seen.add(cohort_id)
            dfs(cohort_id, seen, sorted_cohort_ids)

    return sorted_cohort_ids
