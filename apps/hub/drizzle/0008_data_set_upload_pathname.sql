-- Rows of tokens issued before each token had its own pathname (re-review R1) only counted uploads; they cannot
-- name a pathname, and the count they kept lasts one day, so they are dropped.
DELETE FROM "data_set_upload";--> statement-breakpoint
ALTER TABLE "data_set_upload" ADD COLUMN "pathname" text NOT NULL;--> statement-breakpoint
ALTER TABLE "data_set_upload" ADD CONSTRAINT "data_set_upload_pathname_unique" UNIQUE("pathname");