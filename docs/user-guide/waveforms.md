---
title: Waveforms and distributions
parent: User guide
nav_order: 5
---

# Waveforms and distributions
{: .no_toc }

1. TOC
{:toc}

These are the two metric kinds whose state carries *many* values instead of one. They look alike
and are not:

| | Waveform (`RealTimeSampleArrayMetric`) | Distribution (`DistributionSampleArrayMetric`) |
|---|---|---|
| samples are over | **time** | a **domain**, such as frequency in Hz |
| arrives as | a waveform stream | an episodic metric report |
| behaves like | a moving window; blocks keep coming | one whole picture that gets replaced |
| needs | `Resolution`, `SamplePeriod` | `Resolution`, `DomainUnit` |
| drawn as | a scrolling trace | bars across its range |

The difference between `Unit` and `DomainUnit` is worth seeing: a spectrum is measured in dB
(`Unit`) *across* a range of Hz (`DomainUnit`). The card says *over 0 to 500 Hz* under the plot for
that reason.

**Neither can be remote-controlled.** That is SDC, not the toolbox: no operation takes a sample
array as its argument. A connected device's waveform is marked *samples, read-only*.

## The generator

A waveform with nothing driving it publishes a description and never a sample, which looks like a
broken device. So a new waveform or distribution starts generating at once.

- **Waveforms** draw the curve chosen under **Shape**. *Sine*, *sawtooth*, *square* and *noise*
  test rendering: a sawtooth makes a dropped or doubled block obvious in a way a sine does not.
  *Pulse*, *ECG*, *arterial*, *respiration* and *flow* look like the signals real devices publish;
  they are drawn to be recognisable, not clinical models. The shape is not an SDC concept; it only
  makes a consumer you are testing easy to check by eye. **Cycle** is how many seconds one cycle takes;
  the sample period only sets how finely it is drawn.
- **Distributions** get a bell that drifts across the domain.

Samples go out in blocks of a quarter of a second. Setting a distribution's samples yourself takes
it off the generator, so what you set stays. A distribution has 32 samples across a domain of
positive width. Its `StepWidth` is how far apart two samples sit along the domain; it is not
`Resolution`, which is how finely one sample value is measured.

A sensor that is not *On* (see [Advanced mode](advanced-mode.md)) generates nothing.

## Why the trace moves smoothly

A waveform arrives in blocks, four times a second. Drawing each block as it lands makes the trace
jump. Sending smaller blocks would be allowed, but it would multiply the SOAP messages.

Instead the plot keeps what arrives and reveals it at the pace the samples were taken, which is
what the `SamplePeriod` is for. Two rules keep it honest:

- **Nothing is invented.** When the buffer runs dry the trace stops until the next block. A device
  that has gone quiet looks like one.
- **It cannot drift.** If more than 1.5 s piles up (a hiccup, or a device sending faster than it
  said) it catches up faster than real time until the backlog is gone.

A distribution has no time base, so its bars ease from the old heights to the new ones instead.
