"""Provider adapters shipped with mini-deerflow (P6).

Each module here exports one :class:`~models.base.BaseChatModel` subclass (and
optionally a :class:`~models.base.BaseEmbeddingModel` subclass). New providers
plug in without touching the factory: users declare them in ``models.yaml`` by
their fully qualified class path (e.g. ``models.providers.ark:ArkChatModel``)
and reflection wires them up.
"""
