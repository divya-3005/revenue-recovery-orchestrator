import os
from inngest import Inngest

# In production, this must be True to enforce INNGEST_SIGNING_KEY for secure webhooks
# and INNGEST_EVENT_KEY for sending events.
is_prod = os.getenv("ENVIRONMENT") == "production"
inngest_client = Inngest(app_id="revenue-recovery", is_production=is_prod)
