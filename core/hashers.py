from django.contrib.auth.hashers import PBKDF2PasswordHasher


class StarterPasswordHasher(PBKDF2PasswordHasher):
    """
    Hashes the starter password (= the person's own ID) that sync_roster
    gives every new account. The full-strength default takes ~0.4s per
    account, so a school-sized sync would blow past Vercel's 60s request
    limit; the starter password is guessable anyway, so the extra
    iterations bought nothing. Django re-hashes with the default hasher the
    first time the person logs in (the algorithm name differs from the
    preferred one), so real passwords never stay on this hasher.
    """

    algorithm = "pbkdf2_sha256_starter"
    iterations = 20_000
