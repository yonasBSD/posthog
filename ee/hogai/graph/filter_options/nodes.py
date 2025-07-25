from functools import cached_property
from typing import cast, Optional

from langchain_core.agents import AgentAction
from langchain_core.messages import (
    merge_message_runs,
    ToolMessage as LangchainToolMessage,
    AIMessage as LangchainAIMessage,
)

from langchain_core.prompts import ChatPromptTemplate
from langchain_core.runnables import RunnableConfig
from ee.hogai.graph.base import FilterOptionsBaseNode
from .types import FilterOptionsState, PartialFilterOptionsState
from ee.hogai.graph.query_planner.toolkit import (
    retrieve_entity_properties,
    retrieve_entity_property_values,
    retrieve_event_properties,
    retrieve_event_property_values,
)

from .prompts import (
    FILTER_INITIAL_PROMPT,
    FILTER_FIELDS_TAXONOMY_PROMPT,
    HUMAN_IN_THE_LOOP_PROMPT,
    USER_FILTER_OPTIONS_PROMPT,
    PRODUCT_DESCRIPTION_PROMPT,
    GROUP_PROPERTY_FILTER_TYPES_PROMPT,
    RESPONSE_FORMATS_PROMPT,
    FILTER_LOGICAL_OPERATORS_PROMPT,
    DATE_FIELDS_PROMPT,
    TOOL_USAGE_PROMPT,
    EXAMPLES_PROMPT,
)
from posthog.models.group_type_mapping import GroupTypeMapping
from .toolkit import EntityType, FilterOptionsTool, FilterOptionsToolkit, ask_user_for_help, final_answer

from abc import ABC

from pydantic import ValidationError

from .prompts import (
    REACT_PYDANTIC_VALIDATION_EXCEPTION_PROMPT,
    FILTER_OPTIONS_ITERATION_LIMIT_PROMPT,
)
from ee.hogai.llm import MaxChatOpenAI
from ee.hogai.utils.helpers import format_events_prompt


class FilterOptionsNode(FilterOptionsBaseNode):
    """Node for generating filtering options based on user queries."""

    def __init__(self, team, user, injected_prompts: Optional[dict] = None):
        super().__init__(team, user)
        self.injected_prompts = injected_prompts or {}

    @cached_property
    def _team_group_types(self) -> list[str]:
        return list(
            GroupTypeMapping.objects.filter(project_id=self._team.project.id)
            .order_by("group_type_index")
            .values_list("group_type", flat=True)
        )

    @cached_property
    def _all_entities(self) -> list[str]:
        """Get all available entities as strings."""
        return EntityType.values() + self._team_group_types

    def _get_react_property_filters_prompt(self) -> str:
        return cast(
            str,
            ChatPromptTemplate.from_template(FILTER_FIELDS_TAXONOMY_PROMPT, template_format="mustache")
            .format_messages(groups=self._team_group_types)[0]
            .content,
        )

    def _get_model(self, state: FilterOptionsState):
        return MaxChatOpenAI(
            model="gpt-4.1", streaming=False, temperature=0.3, user=self._user, team=self._team
        ).bind_tools(
            [
                retrieve_entity_properties,
                retrieve_entity_property_values,
                retrieve_event_properties,
                retrieve_event_property_values,
                ask_user_for_help,
                final_answer,
            ],
            tool_choice="required",
            parallel_tool_calls=False,
        )

    def _construct_messages(self, state: FilterOptionsState) -> ChatPromptTemplate:
        """
        Construct the conversation thread for the agent. Handles both initial conversation setup
        and continuation with intermediate steps.
        """
        # Use injected prompts to build dynamic FILTER_INITIAL_PROMPT
        dynamic_filter_prompt = self._get_filter_generation_prompt(self.injected_prompts)

        # Always include the base system and conversation setup
        system_messages = [
            ("system", dynamic_filter_prompt),  # Use dynamic prompt instead of static
            ("system", self._get_react_property_filters_prompt()),
            ("system", HUMAN_IN_THE_LOOP_PROMPT),
        ]

        progress_messages = getattr(state, "tool_progress_messages", [])

        full_conversation = ChatPromptTemplate(
            [*system_messages, ("human", USER_FILTER_OPTIONS_PROMPT), *progress_messages],
            template_format="mustache",
        )
        return full_conversation

    def _get_filter_generation_prompt(self, injected_prompts: dict) -> str:
        return cast(
            str,
            ChatPromptTemplate.from_template(FILTER_INITIAL_PROMPT, template_format="mustache")
            .format_messages(
                **{
                    "product_description_prompt": injected_prompts.get(
                        "product_description_prompt", PRODUCT_DESCRIPTION_PROMPT
                    ),
                    "group_property_filter_types_prompt": injected_prompts.get(
                        "group_property_filter_types_prompt", GROUP_PROPERTY_FILTER_TYPES_PROMPT
                    ),
                    "response_formats_prompt": injected_prompts.get("response_formats_prompt", RESPONSE_FORMATS_PROMPT),
                    "filter_logical_operators_prompt": injected_prompts.get(
                        "filter_logical_operators_prompt", FILTER_LOGICAL_OPERATORS_PROMPT
                    ),
                    "multiple_filters_prompt": injected_prompts.get("multiple_filters_prompt", ""),
                    "date_fields_prompt": injected_prompts.get("date_fields_prompt", DATE_FIELDS_PROMPT),
                    "tool_usage_prompt": injected_prompts.get("tool_usage_prompt", TOOL_USAGE_PROMPT),
                    "examples_prompt": injected_prompts.get("examples_prompt", EXAMPLES_PROMPT),
                }
            )[0]
            .content,
        )

    def run(self, state: FilterOptionsState, config: RunnableConfig) -> PartialFilterOptionsState:
        """Process the state and return filtering options."""
        progress_messages = getattr(state, "tool_progress_messages", [])
        full_conversation = self._construct_messages(state)

        chain = full_conversation | merge_message_runs() | self._get_model(state)

        change = state.change or ""
        current_filters = str(state.current_filters or {})

        # Handle empty change - provide a helpful default task
        if not change.strip():
            change = "Show me all session recordings with default filters"

        events_in_context = []
        if ui_context := self._get_ui_context(state):
            events_in_context = ui_context.events if ui_context.events else []

        # Use injected prompts if available, otherwise fall back to default prompts
        output_message = chain.invoke(
            {
                "core_memory": self.core_memory.text if self.core_memory else "",
                "groups": self._all_entities,
                "change": change,
                "current_filters": current_filters,
                "events": format_events_prompt(events_in_context, self._team),
            },
            config,
        )

        if not output_message.tool_calls:
            raise ValueError("No tool calls found in the output message.")

        tool_call = output_message.tool_calls[0]
        result = AgentAction(tool_call["name"], tool_call["args"], tool_call["id"])
        intermediate_steps = state.intermediate_steps or []

        # Add the new AI message to the progress log
        ai_message = LangchainAIMessage(
            content=output_message.content, tool_calls=output_message.tool_calls, id=output_message.id
        )
        return PartialFilterOptionsState(
            tool_progress_messages=[*progress_messages, ai_message],
            intermediate_steps=[*intermediate_steps, (result, None)],
            generated_filter_options=state.generated_filter_options,
        )


class FilterOptionsToolsNode(FilterOptionsBaseNode, ABC):
    MAX_ITERATIONS = 10  # Maximum number of iterations for the ReAct agent

    def run(self, state: FilterOptionsState, config: RunnableConfig) -> PartialFilterOptionsState:
        toolkit = FilterOptionsToolkit(self._team)
        intermediate_steps = state.intermediate_steps or []
        action, _output = intermediate_steps[-1]
        input = None
        output = ""
        tool_result_msg: list[LangchainToolMessage] = []

        try:
            input = FilterOptionsTool.model_validate({"name": action.tool, "arguments": action.tool_input})
        except ValidationError as e:
            output = str(
                ChatPromptTemplate.from_template(REACT_PYDANTIC_VALIDATION_EXCEPTION_PROMPT, template_format="mustache")
                .format_messages(exception=e.errors(include_url=False))[0]
                .content
            )
        else:
            # First check if we've reached the terminal stage and return the filter options
            if input.name == "final_answer":
                full_response = {
                    "data": input.arguments.data,  # type: ignore
                }

                return PartialFilterOptionsState(
                    generated_filter_options=full_response,
                    intermediate_steps=None,
                )

            # The agent has requested help, so we return a message to the root node
            if input.name == "ask_user_for_help":
                help_message = input.arguments.request  # type: ignore
                return self._get_reset_state(str(help_message), input.name)

        # If we're still here, check if we've hit the iteration limit within this cycle
        if len(intermediate_steps) >= self.MAX_ITERATIONS:
            return self._get_reset_state(FILTER_OPTIONS_ITERATION_LIMIT_PROMPT, "max_iterations")

        if input and not output:
            # Generate progress message before executing tool
            if input.name == "retrieve_entity_property_values":
                output = toolkit.retrieve_entity_property_values(input.arguments.entity, input.arguments.property_name)  # type: ignore
            elif input.name == "retrieve_entity_properties":
                output = toolkit.retrieve_entity_properties(input.arguments.entity)  # type: ignore
            elif input.name == "retrieve_event_property_values":
                output = toolkit.retrieve_event_or_action_property_values(
                    input.arguments.event_name,  # type: ignore
                    input.arguments.property_name,  # type: ignore
                )
            elif input.name == "retrieve_event_properties":
                output = toolkit.retrieve_event_or_action_properties(input.arguments.event_name)  # type: ignore
            else:
                output = toolkit.handle_incorrect_response(input)

        if output:
            tool_context = f"Tool '{action.tool}' was called with arguments {action.tool_input} and returned: {output}"
            tool_msg = LangchainToolMessage(
                content=tool_context,
                tool_call_id=action.log,
            )
            tool_result_msg.append(tool_msg)

        old_msg = getattr(state, "tool_progress_messages", [])
        return PartialFilterOptionsState(
            tool_progress_messages=[*old_msg, *tool_result_msg],
            intermediate_steps=[*intermediate_steps[:-1], (action, output)],
        )

    def router(self, state: FilterOptionsState):
        # If we have a final answer, end the process
        if state.generated_filter_options:
            return "end"

        # Check if we have help request messages (created by _get_reset_state)
        # These are AssistantToolCallMessage instances with specific help content
        if state.intermediate_steps:
            action, _ = state.intermediate_steps[-1]

            if action.tool == "max_iterations" or action.tool == "ask_user_for_help":
                return "end"

        # Continue normal processing - agent should see tool results and make next decision
        return "continue"

    def _get_reset_state(self, output: str, tool_call_id: str) -> PartialFilterOptionsState:
        reset_state = PartialFilterOptionsState.get_reset_state()
        reset_state.intermediate_steps = [
            (
                AgentAction(tool=tool_call_id, tool_input=output, log=""),
                None,
            )
        ]
        return reset_state
