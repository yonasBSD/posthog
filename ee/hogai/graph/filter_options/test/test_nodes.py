from unittest.mock import MagicMock, patch
from parameterized import parameterized
from langchain_core.messages import AIMessage as LangchainAIMessage
from ee.hogai.utils.tests import FakeChatOpenAI

from ee.hogai.graph.filter_options.nodes import FilterOptionsNode, FilterOptionsToolsNode
from ee.hogai.graph.filter_options.types import FilterOptionsState, PartialFilterOptionsState
from posthog.models.group_type_mapping import GroupTypeMapping
from posthog.test.base import BaseTest, ClickhouseTestMixin
from langchain_core.agents import AgentAction
from posthog.schema import (
    MaxRecordingUniversalFilters,
    RecordingDurationFilter,
    PropertyOperator,
    MaxOuterUniversalFiltersGroup,
    MaxInnerUniversalFiltersGroup,
    FilterLogicalOperator,
    PersonPropertyFilter,
    EventPropertyFilter,
    DurationType,
)

AND_FILTER_EXAMPLE = MaxRecordingUniversalFilters(
    duration=[
        RecordingDurationFilter(
            key=DurationType.DURATION,
            operator=PropertyOperator.GTE,
            value=60,
            type="recording",
        )
    ],
    date_from="-3d",
    date_to=None,
    filter_group=MaxOuterUniversalFiltersGroup(
        type=FilterLogicalOperator.AND_,
        values=[
            MaxInnerUniversalFiltersGroup(
                type=FilterLogicalOperator.AND_,
                values=[
                    PersonPropertyFilter(
                        key="$browser",
                        type="person",
                        value=["Mobile"],
                        operator=PropertyOperator.EXACT,
                    )
                ],
            ),
            MaxInnerUniversalFiltersGroup(
                type=FilterLogicalOperator.AND_,
                values=[
                    EventPropertyFilter(
                        key="$login_page",
                        type="event",
                        value=["true"],
                        operator=PropertyOperator.EXACT,
                    )
                ],
            ),
        ],
    ),
)


class TestFilterOptionsNode(ClickhouseTestMixin, BaseTest):
    def setUp(self):
        super().setUp()
        # Create test group type mappings
        GroupTypeMapping.objects.create(
            team=self.team, project=self.project, group_type="organization", group_type_index=0
        )
        GroupTypeMapping.objects.create(team=self.team, project=self.project, group_type="company", group_type_index=1)

    def test_init_without_injected_prompts(self):
        """Test node initializes correctly without injected prompts."""
        node = FilterOptionsNode(self.team, self.user)

        self.assertEqual(node.injected_prompts, {})

    def test_init_with_injected_prompts(self):
        """Test node initializes correctly with injected prompts."""
        injected_prompts = {"test_prompt": "test_value"}
        node = FilterOptionsNode(self.team, self.user, injected_prompts=injected_prompts)

        self.assertEqual(node.injected_prompts, injected_prompts)

    def test_run_successful_execution(self):
        """Test successful run execution with proper tool call creation."""
        tool_calls = [
            {"name": "retrieve_entity_properties", "args": {"arguments": {"entity": "person"}}, "id": "call_123"}
        ]

        # Create a message with the expected tool calls
        message = LangchainAIMessage(content="", tool_calls=tool_calls)
        mock_model = FakeChatOpenAI(responses=[message])

        node = FilterOptionsNode(self.team, self.user)

        with patch.object(node, "_get_model", return_value=mock_model):
            state = FilterOptionsState(
                change="show me user properties",
                current_filters={"property": "active"},
            )

            result = node.run(state, {})

            # Verify result structure
            self.assertIsInstance(result, PartialFilterOptionsState)
            assert result.intermediate_steps is not None
            self.assertEqual(len(result.intermediate_steps), 1)

            # Verify AgentAction creation
            action, output = result.intermediate_steps[0]
            self.assertEqual(action.tool, tool_calls[0]["name"])
            self.assertEqual(action.tool_input, tool_calls[0]["args"])
            self.assertEqual(action.log, tool_calls[0]["id"])
            self.assertIsNone(output)

    def test_run_no_tool_calls_error(self):
        """Test that run raises ValueError when no tool calls are returned."""

        message = LangchainAIMessage(content="No tools available", tool_calls=[])
        mock_model = FakeChatOpenAI(responses=[message])

        node = FilterOptionsNode(self.team, self.user)

        with patch.object(node, "_get_model", return_value=mock_model):
            state = FilterOptionsState(change="test query")

            with self.assertRaises(ValueError) as context:
                node.run(state, {})

            self.assertIn("No tool calls found", str(context.exception))

    def test_run_chain_invoke_parameters(self):
        """Test that the node passes correct parameters and handles responses properly."""
        tool_calls = [{"name": "test_tool", "args": {}, "id": "test_id"}]

        message = LangchainAIMessage(content="", tool_calls=tool_calls)
        mock_model = FakeChatOpenAI(responses=[message])

        node = FilterOptionsNode(self.team, self.user)

        with patch.object(node, "_get_model", return_value=mock_model):
            state = FilterOptionsState(
                intermediate_steps=[], change="filter users by activity", current_filters={"active": True}
            )

            result = node.run(state, {})

            self.assertIsInstance(result, PartialFilterOptionsState)
            assert result.intermediate_steps is not None
            self.assertEqual(len(result.intermediate_steps), 1)

            action, output = result.intermediate_steps[0]
            self.assertEqual(action.tool, "test_tool")
            self.assertEqual(action.tool_input, {})
            self.assertEqual(action.log, "test_id")
            self.assertIsNone(output)

    def test_run_empty_change_default_handling(self):
        """Test that empty change gets replaced with default message."""
        tool_calls = [{"name": "test_tool", "args": {}, "id": "test_id"}]

        # Create a message with the expected tool calls
        message = LangchainAIMessage(content="", tool_calls=tool_calls)
        mock_model = FakeChatOpenAI(responses=[message])

        node = FilterOptionsNode(self.team, self.user)

        with patch.object(node, "_get_model", return_value=mock_model):
            state = FilterOptionsState(intermediate_steps=[], change="")

            result = node.run(state, {})

            self.assertIsInstance(result, PartialFilterOptionsState)
            assert result.intermediate_steps is not None
            self.assertEqual(len(result.intermediate_steps), 1)

    def test_run_preserves_existing_intermediate_steps(self):
        """Test that run preserves existing intermediate steps and adds new ones."""
        tool_calls = [{"name": "new_tool", "args": {"arguments": {"param": "value"}}, "id": "new_id"}]

        # Create a message with the expected tool calls
        message = LangchainAIMessage(content="", tool_calls=tool_calls)
        mock_model = FakeChatOpenAI(responses=[message])

        node = FilterOptionsNode(self.team, self.user)

        # Create existing intermediate step
        existing_action = AgentAction(tool="old_tool", tool_input={}, log="old_log")
        existing_steps: list[tuple[AgentAction, str | None]] = [(existing_action, "previous_result")]

        with patch.object(node, "_get_model", return_value=mock_model):
            state = FilterOptionsState(intermediate_steps=existing_steps, change="continue processing")

            result = node.run(state, {})

            assert result.intermediate_steps is not None
            self.assertEqual(len(result.intermediate_steps), 2)

            # Check existing step is preserved
            old_action, old_output = result.intermediate_steps[0]
            self.assertEqual(old_action.tool, "old_tool")
            self.assertEqual(old_output, "previous_result")

            # Check new step is added
            new_action, new_output = result.intermediate_steps[1]
            self.assertEqual(new_action.tool, "new_tool")
            self.assertEqual(new_action.tool_input, {"arguments": {"param": "value"}})
            self.assertEqual(new_action.log, "new_id")

    def test_injected_prompts_through_public_run_interface(self):
        tool_calls = [{"name": "test_tool", "args": {"arguments": {}}, "id": "test_id"}]

        # Create a message with the expected tool calls
        message = LangchainAIMessage(content="", tool_calls=tool_calls)
        mock_model = FakeChatOpenAI(responses=[message])

        # Create two nodes with different prompt configurations
        default_node = FilterOptionsNode(self.team, self.user)
        custom_node = FilterOptionsNode(
            self.team,
            self.user,
            injected_prompts={
                "examples_prompt": "CUSTOM_MARKER: Special examples",
                "tool_usage_prompt": "CUSTOM_MARKER: Special tool usage",
            },
        )

        state = FilterOptionsState(intermediate_steps=[], change="test query")

        # Test that both node configurations work through the public interface
        # This proves injected prompts don't break functionality
        with (
            patch.object(default_node, "_get_model", return_value=mock_model),
            patch.object(custom_node, "_get_model", return_value=mock_model),
        ):
            # Run both nodes - this proves they work with different configurations
            default_result = default_node.run(state, {})
            custom_result = custom_node.run(state, {})

        # Both should succeed
        self.assertIsInstance(default_result, PartialFilterOptionsState)
        self.assertIsInstance(custom_result, PartialFilterOptionsState)

        # Both should create tool actions (proving the injected prompts didn't break functionality)
        assert default_result.intermediate_steps is not None
        assert custom_result.intermediate_steps is not None
        self.assertEqual(len(default_result.intermediate_steps), 1)
        self.assertEqual(len(custom_result.intermediate_steps), 1)

    def test_intermediate_steps_and_tool_progress_messages_same_length(self):
        """Test that intermediate_steps and tool_progress_messages have the same number of elements."""
        tool_calls = [
            {"name": "retrieve_entity_properties", "args": {"arguments": {"entity": "person"}}, "id": "call_123"}
        ]

        # Create a message with the expected tool calls
        message = LangchainAIMessage(content="", tool_calls=tool_calls)
        mock_model = FakeChatOpenAI(responses=[message])

        node = FilterOptionsNode(self.team, self.user)

        with patch.object(node, "_get_model", return_value=mock_model):
            state = FilterOptionsState(
                change="show me user properties",
                current_filters={"property": "active"},
                tool_progress_messages=[
                    LangchainAIMessage(
                        content="Tool 'retrieve_entity_properties' was called with arguments {'arguments': {'entity': 'person'}} and returned: All the properties",
                        tool_call_id="call_123",
                    )
                ],
                intermediate_steps=[
                    (
                        AgentAction(
                            tool="retrieve_entity_properties",
                            tool_input={"arguments": {"entity": "person"}},
                            log="call_123",
                        ),
                        "All the properties",
                    )
                ],
            )

            result = node.run(state, {})

            # Verify result structure
            self.assertIsInstance(result, PartialFilterOptionsState)
            assert result.intermediate_steps is not None

            # Assert that intermediate_steps and tool_progress_messages have the same length
            self.assertEqual(len(result.intermediate_steps), len(result.tool_progress_messages))

    def test_prompt_includes_defined_events_tag(self):
        """Test that the constructed prompt includes a <defined_events> tag."""
        node = FilterOptionsNode(self.team, self.user)
        state = FilterOptionsState(change="test query")

        # Test that the node's run method uses format_events_prompt correctly
        with patch("ee.hogai.graph.filter_options.nodes.format_events_prompt") as mock_format_events:
            mock_format_events.return_value = "<defined_events><event><name>test_event</name></event></defined_events>"

            # Create a custom fake model that captures the invoke call
            class CapturingFakeChatOpenAI(FakeChatOpenAI):
                def __init__(self, *args, **kwargs):
                    super().__init__(*args, **kwargs)
                    self.__dict__["captured_invoke_calls"] = []

                def invoke(self, input, config=None, **kwargs):
                    self.__dict__["captured_invoke_calls"].append((input, config, kwargs))
                    return super().invoke(input, config, **kwargs)

            tool_calls = [{"name": "test_tool", "args": {}, "id": "test_id"}]
            message = LangchainAIMessage(content="", tool_calls=tool_calls)
            capturing_model = CapturingFakeChatOpenAI(responses=[message])

            with patch.object(node, "_get_model", return_value=capturing_model):
                node.run(state, {})

                # Verify format_events_prompt was called with correct parameters
                mock_format_events.assert_called_once()
                call_args = mock_format_events.call_args
                self.assertEqual(
                    call_args[0][0], [], "format_events_prompt should be called with empty events_in_context"
                )
                self.assertEqual(call_args[0][1], self.team, "format_events_prompt should be called with team")

                # Verify the chain.invoke was called and the messages contain <defined_events> tag
                self.assertEqual(
                    len(capturing_model.__dict__["captured_invoke_calls"]), 1, "chain.invoke should be called once"
                )
                invoke_input = capturing_model.__dict__["captured_invoke_calls"][0][0]
                self.assertIsInstance(invoke_input, list, "chain.invoke should be called with a list of messages")

                # Check that the messages contain the <defined_events> tag
                messages_content = " ".join(str(msg.content) for msg in invoke_input)
                self.assertIn("<defined_events>", messages_content, "messages should contain <defined_events> tag")
                self.assertIn(
                    "<event><name>test_event</name></event>",
                    messages_content,
                    "messages should contain the mocked event",
                )


class TestFilterOptionsToolsNode(ClickhouseTestMixin, BaseTest):
    def test_router_with_generated_filter_options(self):
        """Test router returns 'end' when filter options are generated."""
        node = FilterOptionsToolsNode(self.team, self.user)
        state = FilterOptionsState(intermediate_steps=[], generated_filter_options={"result": "filter", "data": {}})

        result = node.router(state)

        self.assertEqual(result, "end")

    def test_router_with_help_request_message(self):
        """Test router returns 'end' for help request messages."""
        node = FilterOptionsToolsNode(self.team, self.user)
        # Create state with intermediate steps that have help request action
        action = AgentAction(tool="ask_user_for_help", tool_input="Need help with filters", log="")
        state = FilterOptionsState(intermediate_steps=[(action, None)])

        result = node.router(state)

        self.assertEqual(result, "end")

    def test_router_with_max_iterations_message(self):
        """Test router returns 'end' for max iterations message."""
        node = FilterOptionsToolsNode(self.team, self.user)
        # Create state with intermediate steps that have max iterations action
        action = AgentAction(tool="max_iterations", tool_input="Reached maximum iterations", log="")
        state = FilterOptionsState(intermediate_steps=[(action, None)])

        result = node.router(state)

        self.assertEqual(result, "end")

    def test_router_continue_normal_processing(self):
        """Test router returns 'continue' for normal processing."""
        node = FilterOptionsToolsNode(self.team, self.user)
        state = FilterOptionsState(intermediate_steps=[])

        result = node.router(state)

        self.assertEqual(result, "continue")

    @parameterized.expand(
        [
            ["final_answer", {"name": "final_answer", "arguments": {"data": AND_FILTER_EXAMPLE}}],
            ["ask_user_for_help", {"name": "ask_user_for_help", "arguments": {"request": "Need clarification"}}],
            [
                "retrieve_entity_property_values",
                {"name": "retrieve_entity_property_values", "arguments": {"entity": "person", "property_name": "name"}},
            ],
            ["retrieve_entity_properties", {"name": "retrieve_entity_properties", "arguments": {"entity": "person"}}],
        ]
    )
    @patch("ee.hogai.graph.filter_options.nodes.FilterOptionsToolkit")
    def test_run_handles_different_tool_calls(self, tool_name, tool_args, mock_toolkit_class):
        """Test run method handles different tool calls correctly."""

        # Setup mock toolkit
        mock_toolkit = MagicMock()
        mock_toolkit.retrieve_entity_property_values.return_value = "All the property values"
        mock_toolkit.retrieve_entity_properties.return_value = "All the properties"
        mock_toolkit_class.return_value = mock_toolkit

        node = FilterOptionsToolsNode(self.team, self.user)
        action = AgentAction(tool=tool_name, tool_input=tool_args["arguments"], log="test")
        state = FilterOptionsState(intermediate_steps=[(action, None)])

        if tool_name == "final_answer":
            result = node.run(state, {})
            assert result.generated_filter_options is not None  # Type guard
            self.assertEqual(result.generated_filter_options["data"], AND_FILTER_EXAMPLE)
        elif tool_name == "ask_user_for_help":
            result = node.run(state, {})
            # Should return reset state with help message in intermediate_steps
            assert result.intermediate_steps is not None
            self.assertEqual(len(result.intermediate_steps), 1)
            action, output = result.intermediate_steps[0]
            self.assertEqual(action.tool, "ask_user_for_help")
            self.assertEqual(action.tool_input, "Need clarification")
        elif tool_name == "retrieve_entity_property_values":
            result = node.run(state, {})
            mock_toolkit.retrieve_entity_property_values.assert_called_once_with(
                tool_args["arguments"]["entity"], tool_args["arguments"]["property_name"]
            )
            assert result.intermediate_steps is not None
            self.assertEqual(result.intermediate_steps[0][1], "All the property values")
        elif tool_name == "retrieve_entity_properties":
            result = node.run(state, {})
            mock_toolkit.retrieve_entity_properties.assert_called_once_with(tool_args["arguments"]["entity"])
            assert result.intermediate_steps is not None
            self.assertEqual(result.intermediate_steps[0][1], "All the properties")
