"""Model-facing delegation dispatch, before unsupported arguments can be filtered."""


def dispatch_delegate_task(agent, function_args: dict) -> str:
    """Forward supported fields only after refusing unsupported raw routing intent."""
    from tools.delegate_tool import _strip_model_hidden_task_fields, delegate_task
    from tools.delegate_tool_routes import raw_routing_error
    from tools.registry import tool_error

    error = raw_routing_error(function_args, parent_agent=agent)
    if error:
        return tool_error(error)
    # Top-level model delegations run in the background unless the session joins them;
    # orchestrator children remain synchronous. The model cannot choose background mode.
    return delegate_task(
        goal=function_args.get("goal"), context=function_args.get("context"),
        tasks=_strip_model_hidden_task_fields(function_args.get("tasks")),
        max_iterations=function_args.get("max_iterations"), role=function_args.get("role"),
        background=not (getattr(agent, "_delegate_depth", 0) > 0), images=function_args.get("images"),
        action=function_args.get("action"),
        subagent_id=function_args.get("subagent_id"), message=function_args.get("message"), parent_agent=agent,
    )


def dispatch_registry_delegate_task(args: dict, parent_agent=None) -> str:
    """Registry fallback keeps its existing background/output-schema policy and the same guard."""
    from tools.delegate_tool import _model_background_value, _strip_model_hidden_task_fields, delegate_task
    from tools.delegate_tool_routes import raw_routing_error
    from tools.registry import tool_error

    error = raw_routing_error(args, parent_agent=parent_agent)
    if error:
        return tool_error(error)
    return delegate_task(
        goal=args.get("goal"), context=args.get("context"), tasks=_strip_model_hidden_task_fields(args.get("tasks")),
        max_iterations=args.get("max_iterations"), role=args.get("role"),
        background=_model_background_value(args, parent_agent), output_schema=args.get("output_schema"),
        images=args.get("images"), action=args.get("action"), subagent_id=args.get("subagent_id"),
        message=args.get("message"), parent_agent=parent_agent,
    )
