---
title: Privacy policy
draft: false
---

Last updated: 5 November 2026

This policy says what Jeff collects, why, where it is kept and for how long. It covers the Jeff website (jeffhub.ai), the Jeff API and adapter requests.

## Who we are

Jeff is run by Casperaki Multimedia Solutions Ltd. (company number 10634954), whose registered office is 40 Bank Street, Level 19, London, E14 5NR, England. We decide what is collected and why.

For any question about your data, or to use any of your rights below, write to mstrasser@scissero.com.

## Your account

You sign in with a link sent to your email, or with Google or GitHub. Sign-in is handled by Better Auth, running in our own site. We keep:

- your email address and your name;
- the address of your profile picture, if Google or GitHub gives us one;
- which sign-in methods you used. If you sign in with Google or GitHub, we also keep the tokens they give us, encrypted. We do not use them;
- your sessions: when each started, and the IP address and browser name it came from. A session lasts 30 days, or until you sign out;
- your API keys: the name you gave each one, the first 6 characters after its prefix, and a fingerprint (a hash) of the whole key. We never keep the key itself. We also keep when each key was made, revoked and last used;
- how many input tokens you used in each billing period.

We use these to sign you in, to run your account, to check your API keys, and to count your usage against your allowance.

## Payments

Payments go through Stripe. You type your card details into Stripe's own page; we never see or keep them. We give Stripe your email address and your account's ID. When tax applies, Stripe also asks for your billing address. We keep your Stripe customer ID and the state of your plan: whether it is active, and when the current period starts and ends.

## Email

We send email through SendGrid. We send you a sign-in link when you ask for one. When you send an adapter request, we email ourselves its number and a link to it, never its examples.

To limit abuse, we count the sign-in links sent to each address in the last hour. We keep only a fingerprint (a hash) of the address for this, and delete it after an hour. We also count sign-in requests from each IP address over a short time.

## Bot checks

The email sign-in form, and the playground for visitors who are not signed in, use Cloudflare Turnstile to check that a person is there. Turnstile runs in your browser and sends Cloudflare information about your browser and your IP address. Cloudflare's own privacy policy covers that.

To hold the playground to a few runs a minute, we keep a fingerprint (a hash) of your IP address for one minute. After a visitor passes the bot check, a cookie remembers the pass for one hour.

## Cookies and your browser

- Signing in sets the cookies Better Auth needs to sign you in and keep you signed in. Scripts on the page cannot read them.
- The playground sets the one-hour cookie above, for visitors who are not signed in.
- The site remembers your choice of code language, and whether code runs on your machine or through the API, in your browser's own storage. That never leaves your browser.
- The site uses no analytics and no advertising trackers.

## Hosting

The website runs on Vercel. Our database is Neon Postgres, through Vercel. The data sets sent with adapter requests are kept in a private Vercel Blob store. The website's functions, the database and the data sets are all in London, United Kingdom. Vercel keeps its request logs for the site for up to 30 days.

## The API

The API runs on our own servers, in London, United Kingdom.

- We do not store what you send to the API, or the answers it gives.
- For each call we log the time, your account's ID, your IP address, the address it was forwarded from, the method and path called, the status of the answer, and your program's user agent. We do not look up where an IP address is.
- We count the input tokens each account uses in each billing period, and when each key was last used. The API sends these totals to our website, to show your usage and stop calls when the allowance is used up.
- If a call fails in an unexpected way, our error log keeps the error's message. It is not meant to hold what you sent, but an error message can quote part of it.
- We keep the API's call log and error log for 90 days, then delete them.

The playground sends its examples, with any changes you make to them, through the API in the same way.

## Open adapters on your own machine

The SDKs and the engine run on your machine. Running an open adapter there sends nothing to us. Its weights download from Hugging Face, under Hugging Face's own privacy policy.

## Adapter requests

When you ask for an adapter, we keep the form you filled in, and your examples or your data set.

- We give your examples to our own language model, to write the training data. They are not sent to any outside service.
- If you tick the optional box, we may also use your examples to improve Jeff's base model, the model every adapter is built on. Without the tick, we use them only for your adapter.
- We keep your examples until the adapter is delivered and for 90 days after; a daily job then deletes them, and your data set.
- We delete them sooner if you ask: write to the privacy address above.
- When you send a request, you confirm that the examples contain no personal data, or that it has been removed.

## Deleting your account

You can delete your account on your account page. This cancels your plan at once, and deletes your account, your sessions, your sign-in methods, your API keys, your usage, and your adapter requests with their examples and data sets.

Stripe keeps its own records of your payments, such as invoices. Deleted data can stay in the database's backups for up to 7 days.

## Your rights

The UK General Data Protection Regulation and the Data Protection Act 2018 apply. We use your data to run your account and bill you (to perform our contract with you), to keep the service secure and to understand its use (our legitimate interests), and, only if you tick the box on an adapter request, to improve Jeff's base model (your consent, which you can withdraw at any time). You may ask us for a copy of your data, and ask us to correct it, delete it, stop using it, or send it to you in a form a machine can read. You can also complain to the Information Commissioner's Office (ico.org.uk).

## Children

Jeff is not for children. You must be at least 18 to make an account.

## Changes to this policy

We will post any new version here and change the date at the top. We tell account holders by email about any material change.
