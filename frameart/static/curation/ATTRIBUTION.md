# Visual Art Profile quiz artwork

These 24 examples are preference prompts, not the personalized review collection.
All are sourced from the Art Institute of Chicago's open-access collection. Every
record was checked through its primary museum API and explicitly returned
`is_public_domain: true` when this quiz catalog was built on 2026-11-12.

The bundled `quiz.json` preserves the museum artwork title, artist, date, record
URL, API URL, image URL, public-domain declaration, and CC0 license URL for every
image. Museum metadata other than descriptions is designated CC0 by the API.
These files do not reproduce museum curatorial descriptions.

Images were downloaded sequentially from the museum's recommended 843-pixel IIIF
endpoint, then resized without cropping to a maximum edge of 640 pixels and
encoded as WebP. The 24 bundled previews total 930,512 bytes. They are suitable
for this web quiz; a future television collection should fetch and prepare the
high-resolution original separately.

The pair prompts and preference tags are FrameArt's editorial interpretation of
the examples. In particular, seasonal tags indicate a visual mood or palette;
they do not assert when an artwork was painted. The quiz includes both familiar
and modern styles but does not claim to cover every tradition or contemporary
art movement. The household's written answers can add interests not pictured.

Primary sources:

- [Art Institute of Chicago API documentation](https://api.artic.edu/docs/)
- [Art Institute of Chicago Open Access](https://www.artic.edu/open-access/open-access-images)
- [CC0 1.0](https://creativecommons.org/publicdomain/zero/1.0/)
