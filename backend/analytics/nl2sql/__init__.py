"""NL2SQL runtime package for the analytics workbench.

Keep package imports lightweight: entity-candidate APIs should not import the
Vanna/Milvus runtime unless a caller explicitly asks for it.
"""

__all__ = ["VannaRuntimeConfig", "build_vanna_client", "build_vanna_client_from_app_config"]


def __getattr__(name: str):
    if name in __all__:
        from .runtime import VannaRuntimeConfig, build_vanna_client, build_vanna_client_from_app_config

        exports = {
            "VannaRuntimeConfig": VannaRuntimeConfig,
            "build_vanna_client": build_vanna_client,
            "build_vanna_client_from_app_config": build_vanna_client_from_app_config,
        }
        return exports[name]
    raise AttributeError(name)
