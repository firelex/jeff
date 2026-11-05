// /pricing (spec Section 7): running Jeff locally is free; the API is one plan with a monthly allowance of input tokens
// and a hard stop. Prerendered like every page of the hub: the price and the allowance are the plan's settings, read
// at build time (public-settings.server.ts), never written here.
import { Link, useLoaderData } from 'react-router';
import { BUTTON_PRIMARY, CARD, PAGE_TITLE, SECTION_HEADING, TEXT_LINK } from '../components/styles';
import { priceText, publicSettings, tokensText } from '../lib/public-settings.server';

export function meta() {
  return [
    { title: 'Pricing | Jeff' },
    { name: 'description', content: 'Jeff is free on your own machine. The API is one monthly plan with an allowance of input tokens.' },
  ];
}

export function loader() {
  const shown = publicSettings();
  return { price: priceText(shown.priceUsd), allowance: tokensText(shown.allowanceInputTokens), tax: shown.automaticTax };
}

// The comparison line names a competitor: the owner approved its wording.
export default function Pricing() {
  const { price, allowance, tax } = useLoaderData<typeof loader>();
  // With Stripe's automatic tax on, VAT or sales tax is added to the price at Checkout (billing.server.ts).
  const most = tax ? `${price} a month, plus any VAT or sales tax` : `${price} a month`;
  return (
    <main id="main" className="mx-auto max-w-4xl px-4 py-12 sm:py-16">
      <h1 className={PAGE_TITLE}>Pricing</h1>
      <div className="mt-8 grid gap-6 md:grid-cols-2">
        <section className={`flex flex-col p-5 sm:p-6 ${CARD}`} aria-labelledby="pricing-local">
          <h2 id="pricing-local" className={SECTION_HEADING}>Local</h2>
          <p className="mt-3 text-text">Free forever, every adapter, the SDKs and the engine on your machine.</p>
          <div className="mt-auto pt-6">
            <Link to="/docs/guides/quickstart" className={TEXT_LINK}>Read the quick start</Link>
          </div>
        </section>
        <section className={`flex flex-col p-5 sm:p-6 ${CARD}`} aria-labelledby="pricing-api">
          <h2 id="pricing-api" className={SECTION_HEADING}>API</h2>
          <p className="mt-3 text-text">{`${price} a month, ${allowance} input tokens a month, every adapter, cancel any time.`}</p>
          <p className="mt-3 text-sm text-soft">
            {`When the allowance is used up, calls are refused until your next billing period starts: you are never charged more than ${most}.`}
          </p>
          <div className="mt-auto pt-6">
            <Link to="/signup" className={`${BUTTON_PRIMARY} px-4 py-2`}>Get an API key</Link>
          </div>
        </section>
      </div>
      <p className="mt-6 text-sm text-soft">Half TypeSafe's price per token at the full allowance.</p>
      <p className="mt-3 text-sm text-soft">
        Need a decision that is not in the catalogue? <Link to="/request-adapter" className={TEXT_LINK}>Request an adapter</Link>.
      </p>
    </main>
  );
}
