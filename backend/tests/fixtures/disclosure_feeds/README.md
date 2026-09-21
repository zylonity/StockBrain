# Disclosure feed captures

The initial captures accompany the approved 2026-09-21 design.

`globenewswire_canada.xml` is an HTML 404 response, despite the handoff describing it as RSS. Keep it as a malformed-response fixture.

`globenewswire_canada_valid.xml` was captured on 2026-09-21 from:

https://www.globenewswire.com/RssFeed/country/Canada/feedTitle/GlobeNewswire%20-%20News%20from%20Canada

It contains 20 RSS items, first release `3365350` and last release `3364980`. Use this capture for Canadian issuer/ticker parsing tests. Tests read these files locally and never refresh them implicitly.
