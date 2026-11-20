-- Swedish and English versions of the same Migrationsverket page (D6), as the site pairs
-- them with hreflang links. Pages pair as a whole; their sections are separate units.
CREATE TABLE page_translation (
  document     text NOT NULL,                       -- page URL
  language     text NOT NULL CHECK (language IN ('sv', 'en')),
  translation  text NOT NULL,                       -- the other language's page URL
  PRIMARY KEY (document, language)
);
