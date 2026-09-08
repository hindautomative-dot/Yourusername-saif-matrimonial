"""
Load test for Saif Matrimonial — run against a STAGING copy, never
production, since this generates real registration/unlock-request rows.

Install:  pip install locust
Run:      locust -f locustfile.py --host https://staging.yourdomain.com
Then open http://localhost:8089, set e.g. 80 users / 5 per second spawn
rate, and start. Watch the "Response times" and "Failures" charts —
that's your "requests/sec before things degrade" number for the README.

This simulates the traffic pattern described in the brief: most users
browsing/filtering profiles, a smaller number submitting registrations
or unlock requests at the same time.
"""
import random
from locust import HttpUser, task, between


CITIES = ["Kolkata", "Mumbai", "Delhi", "Hyderabad", "Lucknow", ""]


class BrowsingUser(HttpUser):
    """~80% of simulated traffic: people browsing and filtering profiles.
    This is the read-heavy path WAL mode + indexes + the TTL cache are
    meant to keep fast under concurrency."""
    weight = 8
    wait_time = between(1, 4)

    @task(3)
    def homepage(self):
        self.client.get("/", name="/ (home)")

    @task(5)
    def browse_filtered(self):
        params = {"city": random.choice(CITIES)}
        self.client.get("/browse", params=params, name="/browse?city=..")

    @task(2)
    def view_profile(self):
        # Adjust the profile_code pattern to match real seeded staging data.
        code = f"SMS{random.randint(1, 200)}"
        self.client.get(f"/profile/{code}", name="/profile/<code>")

    @task(1)
    def faq_page(self):
        self.client.get("/faq", name="/faq")


class SubmittingUser(HttpUser):
    """~20% of simulated traffic: people mid-registration or submitting an
    unlock request/payment proof — the write-heavy path that WAL +
    busy_timeout + the rate limiter are meant to protect."""
    weight = 2
    wait_time = between(3, 8)

    @task
    def view_register_form(self):
        self.client.get("/register-yourself", name="/register-yourself (GET)")

    # A POST-with-file-upload task is deliberately left commented out —
    # wire it up with real staging-safe test data (dummy image, throwaway
    # phone number range) before running, so you don't spam the OTP/
    # notification pipeline with hundreds of fake submissions per run.
    #
    # @task
    # def submit_registration(self):
    #     with open("test_photo.jpg", "rb") as f:
    #         self.client.post(
    #             "/register-yourself",
    #             data={...},
    #             files={"photo": f},
    #             name="/register-yourself (POST)",
    #         )
