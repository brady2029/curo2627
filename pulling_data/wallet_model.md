# How the wallet model works

The model does not start out knowing who is a market maker. It learns which wallets trade like each other. The market-maker label is added afterward, by splitting those results into two groups.

## What went in

Every wallet that traded in the 15 Polymarket markets got its history across Polymarket, from 2023-09-01 through 2026-09-29. Each history is capped at the most recent 2,000 trades. A wallet needs at least 20 trades to be used. That left 16,938 wallets.

Each trade becomes 8 numbers:

1. Dollar size, on a log scale. The value stored is ln(1 + dollars). A $10 trade becomes ln(11), about 2.4. A $1,000 trade becomes ln(1,001), about 6.9. In raw dollars the second trade is 100 times the first. On the log scale it is about 3 times as large. The model still knows the $1,000 trade is bigger. It does not treat it as 100 times more important. The same compression is used for the time gap in item 5.
2. Price, from 0 to 1.
3. Buy or sell, as 1 or 0.
4. Yes or not-Yes, as 1 or 0. This is only a check for the word Yes. If the outcome is Yes, the number is 1. If it is No, the number is 0. If it is a name, such as a team, the number is also 0. No and a team name are not being called the same bet. They are both answers to "was this the Yes token?" and that answer is no.
5. Minutes since that wallet's previous trade, on the same log scale as dollar size.
6. and 7. The hour of the day, written as a circle so 11pm sits next to midnight.
8. Share size, on the same log scale. This is the number of shares, not the dollars. A trade of 10 shares becomes about 2.4. A trade of 1,000 shares becomes about 6.9. Dollar size and share size are both kept because a 1,000-share trade at 5¢ is not the same dollar amount as a 1,000-share trade at 95¢.

The numbers are rescaled so each one has mean 0 and spread 1, using the training wallets only.

## The network

A wallet is a sequence, not one row. The model reads the trades in order with a GRU, which is a small recurrent network. It keeps a running summary and updates that summary at every trade. After the last trade, the summary is a list of 64 numbers. A short second layer shrinks that to 32 numbers. Those 32 numbers are the wallet's embedding. They are scaled to length 1, so two wallets are compared by the angle between them, not by how large the numbers are.

Only 50 trades are shown at a time. If a wallet has more, the model sees a 50-trade slice. Every wallet counts once per pass, so a 2,000-trade wallet does not outweigh a 30-trade wallet.

## How it learned

There are no market-maker labels during training. The lesson is: two slices from the same wallet should match, and slices from different wallets should not.

In each step the model sees 64 wallets. It takes two random 50-trade slices from each wallet, turns each slice into a 32-number embedding, and scores every pair. The matching pair is the two slices of the same wallet. The loss (NT-Xent) punishes the model when a slice looks more like some other wallet than like its own pair.

80% of wallets are used to learn. 20% are held out, seed 42, and a wallet is entirely on one side or the other. After 8 passes, the training loss fell from 3.27 to 2.25 and the holdout loss fell from 2.81 to 2.24. A random pairing would score about 4.8, so the model did pick up real habits, and those habits showed up on wallets it was not trained on.

The saved file is `wallet_encoder.pt`. Running the trained model on each wallet's most recent 50 trades writes `data_polymarket/wallet_embeddings.csv`, one row per wallet, 32 numbers per row.

## Where the market-maker label comes from

The network never outputs "market maker." After the embeddings exist, the 16,938 points are split into two groups with k-means (seed 42). The group whose wallets trade more often is labeled Market Maker. The other is labeled Non Market Maker. That label is the `group` column.

That split is 6,725 market makers and 10,213 non-market-makers. The market-maker group is the one that buys and sells the same contract about a minute apart, usually at a price already near 95 cents. The names were checked against the trades after the split. They were not given to the model as answers.

490 wallets have fewer than 20 trades. They have no embedding and no group.

## The numbers

Data:

- 15 Polymarket markets. Wallets that traded in them: 17,428.
- Trades inside those 15 markets only: 62,718, totaling $4,041,577. This is the study tape, not the training set.
- History window: 2023-09-01 through 2026-09-29.
- Cap: the most recent 2,000 trades per wallet. The pull reads up to 8 pages of 1,000 trades to fill that cap.
- Full histories saved: 6,885,405 trades. 698 wallets hit the 2,000 cap.
- Minimum to enter the model: 20 trades. 16,938 wallets cleared it. 490 did not, and they have no label.

Network:

- 8 numbers per trade. 50 trades shown at a time.
- GRU summary: 64 numbers, then shrunk to 32. Those 32 are scaled to length 1.
- Batch: 64 wallets, two slices each, so 128 embeddings compared at once.
- Temperature 0.2. Learning rate 0.001. 8 passes. Seed 42.
- Split: 80% train, 20% holdout, which is 13,550 train wallets and 3,388 holdout wallets. A wallet is entirely on one side.

Loss, train then holdout. A random pairing in a batch of 64 would score about 4.8.

| Pass | Train | Holdout |
|---|---|---|
| 1 | 3.268 | 2.813 |
| 2 | 2.709 | 2.609 |
| 3 | 2.565 | 2.478 |
| 4 | 2.460 | 2.399 |
| 5 | 2.388 | 2.350 |
| 6 | 2.339 | 2.327 |
| 7 | 2.307 | 2.280 |
| 8 | 2.247 | 2.236 |

Labels, after training:

- K-means into 2 groups, seed 42, 40 steps.
- Market Maker: 6,725. Non Market Maker: 10,213.
- Those embeddings cover 96.8% of the dollars in the 15 markets.
- A hand-label queue of 80 wallets was never filled, so no supervised institutional score was used.

## What this does not do

The model does not use who won a market, profit, or a hand-made list of institutions. It does not label anyone informed or retail. Two wallets land near each other because their recent trades look alike in price, side, size, and timing.
